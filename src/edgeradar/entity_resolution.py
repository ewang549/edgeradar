"""Entity resolution: group markets across platforms that describe the SAME event.

This is the hard, high-value part of EdgeRadar. Manifold and Kalshi word the same
real-world event completely differently, so before we can compare their prices we
must decide which markets refer to the same thing and assign them a shared
``event_id``.

The approach is deliberately layered (and honest about its limits):

1. Feature extraction — normalize each title to tokens, guess a coarse category
   (sports / weather / politics / econ / crypto / other), and read the close date.
2. Blocking — only compare markets within the same category. This is the standard
   trick to avoid an O(n^2) comparison of everything-to-everything and to cut
   obvious non-matches.
3. Fuzzy scoring — for each candidate pair, combine a token-set Jaccard with a
   sequence-similarity ratio, plus a small bonus when close dates line up. The
   result is a confidence in [0, 1].
4. Manual overrides — a human-maintained table can force a match or forbid one,
   always winning over the fuzzy score (with confidence 1.0). This is how
   ambiguous real cases get resolved without weakening the automatic logic.
5. Clustering — union-find over accepted pairs groups markets into events; each
   market (even an unmatched one) lands in exactly one ``event_id``.

Every market keeps the confidence by which it joined its event, and near-threshold
pairs are surfaced for review. A natural future upgrade is an LLM-assisted matcher
for the fuzzy tier (embed titles, propose candidate pairs for human confirmation);
the override table is exactly where those confirmations would be recorded.

This module uses only the standard library + pandas — no heavyweight matching
dependency — so the logic is transparent and easy to audit.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from edgeradar.config import get_settings
from edgeradar.resolution_diagnostics import (
    ResolutionDiagnostics,
    compute_resolution_diagnostics,
    write_resolution_diagnostics,
)
from edgeradar.storage import read_quotes

# Words that carry no matching signal; dropped before comparison.
STOPWORDS = {
    "will",
    "the",
    "a",
    "an",
    "be",
    "is",
    "are",
    "to",
    "of",
    "on",
    "in",
    "at",
    "by",
    "for",
    "and",
    "or",
    "this",
    "that",
    "it",
    "with",
    "have",
    "has",
    "do",
    "does",
    "than",
    "more",
    "less",
    "least",
    "above",
    "below",
    "over",
    "under",
    "yes",
    "no",
}

# Coarse category keyword buckets. First bucket with a hit wins. Expanded from the
# original list with terms found on live data (World Cup, championships, macro,
# politics) so fewer real markets fall into the catch-all "other" bucket.
CATEGORY_KEYWORDS: dict[str, set[str]] = {
    "weather": {
        "temperature",
        "temp",
        "high",
        "low",
        "rain",
        "snow",
        "degrees",
        "fahrenheit",
        "weather",
        "82.5f",
    },
    "sports": {
        "beat",
        "defeat",
        "win",
        "wins",
        "winner",
        "game",
        "vs",
        "celtics",
        "lakers",
        "nba",
        "nfl",
        "mlb",
        "nhl",
        "fifa",
        "world_cup",
        "championship",
        "finals",
        "playoff",
        "group",
        "tournament",
        "season",
        "match",
    },
    "politics": {
        "election",
        "president",
        "presidential",
        "senate",
        "ticket",
        "democratic",
        "republican",
        "governor",
        "vote",
        "ossoff",
        "warnock",
        "impeach",
        "nomination",
        "ballot",
        "candidate",
        "approval",
        "congress",
        "house",
        "primary",
    },
    "econ": {
        "retail",
        "sales",
        "cpi",
        "inflation",
        "gdp",
        "rate",
        "fed",
        "fomc",
        "unemployment",
        "earnings",
        "jobs",
        "treasury",
        "recession",
    },
    "crypto": {"bitcoin", "btc", "ethereum", "eth", "crypto", "solana", "dogecoin", "ripple"},
}

DEFAULT_THRESHOLD = 0.60  # confidence at/above which a fuzzy pair is accepted
REVIEW_BAND = 0.12  # pairs within this much below threshold are flagged for review
# Stricter bar for a pair where at least one side has no recognized entity AND no
# capitalized subject — see `_score_pair` in `resolve`.
GENERIC_MATCH_THRESHOLD = 0.92

# --------------------------------------------------------------------------- #
# Alias normalization + entity gazetteer
#
# Different platforms word the same entity differently ("USA" vs "United States",
# "BTC" vs "Bitcoin", "NYC" vs "New York"). We fold known aliases onto one
# canonical, underscore-joined token *before* tokenizing, so title_similarity sees
# them as identical tokens. The same canonical tokens double as a small gazetteer
# (ENTITY_VOCAB) used to sub-block comparisons by extracted entity (see `resolve`):
# this is what stops two genuinely different countries' near-identical "Will X win
# Group Y" titles from ever being compared (and, via transitive clustering,
# collapsing into one giant false event — a real bug found by running this
# resolver against live World Cup data, see FINDINGS.md).
# --------------------------------------------------------------------------- #

ALIASES: dict[str, str] = {
    # countries / regions. NOTE: bare "america" is deliberately NOT aliased here —
    # it's ambiguous with "North America"/"South America" (FIFA confederations),
    # which must stay distinct from the country (found on live World Cup data).
    "usa": "united_states",
    "us": "united_states",
    "united states": "united_states",
    "uk": "united_kingdom",
    "britain": "united_kingdom",
    "great britain": "united_kingdom",
    "united kingdom": "united_kingdom",
    "uae": "united_arab_emirates",
    "south korea": "south_korea",
    "korea republic": "south_korea",
    "ivory coast": "cote_divoire",
    "cote d'ivoire": "cote_divoire",
    "bosnia": "bosnia_and_herzegovina",
    "bosnia and herzegovina": "bosnia_and_herzegovina",
    "new zealand": "new_zealand",
    "saudi arabia": "saudi_arabia",
    "dominican republic": "dominican_republic",
    "cape verde": "cape_verde",
    "trinidad and tobago": "trinidad_and_tobago",
    "turkiye": "turkey",
    # crypto tickers
    "btc": "bitcoin",
    "xbt": "bitcoin",
    "eth": "ethereum",
    "sol": "solana",
    "doge": "dogecoin",
    "xrp": "ripple",
    # cities
    "nyc": "new_york",
    "new york city": "new_york",
    "la": "los_angeles",
    "sf": "san_francisco",
    "san fran": "san_francisco",
    "washington dc": "washington",
    "vegas": "las_vegas",
    "nola": "new_orleans",
}

# Additional recognized entities with no alias needed (already a single canonical
# token once lowercased) — common World Cup nations, US sports-franchise cities,
# and weather cities. Not exhaustive; unrecognized names simply get no entity
# (see `extract_entities`), which is a safe, documented blocking trade-off, not a
# silent failure.
_EXTRA_ENTITIES = {
    # countries (2026 World Cup field + common politics/econ mentions)
    "brazil",
    "argentina",
    "germany",
    "spain",
    "japan",
    "uruguay",
    "morocco",
    "ecuador",
    "belgium",
    "netherlands",
    "norway",
    "england",
    "canada",
    "mexico",
    "panama",
    "tunisia",
    "algeria",
    "colombia",
    "paraguay",
    "austria",
    "croatia",
    "sweden",
    "haiti",
    "uzbekistan",
    "jordan",
    "ghana",
    "senegal",
    "egypt",
    "nigeria",
    "australia",
    "portugal",
    "france",
    "italy",
    "poland",
    "iran",
    "iraq",
    "china",
    "india",
    "russia",
    "ukraine",
    "switzerland",
    "scotland",
    "wales",
    "ireland",
    "chile",
    "peru",
    "bolivia",
    "venezuela",
    "honduras",
    "curacao",
    "jamaica",
    "qatar",
    "kuwait",
    "oman",
    "thailand",
    "vietnam",
    "indonesia",
    "malaysia",
    "philippines",
    "greece",
    "romania",
    "hungary",
    "czechia",
    "slovakia",
    "finland",
    "denmark",
    "iceland",
    "serbia",
    # US sports-franchise / weather cities not already covered by ALIASES values
    "boston",
    "chicago",
    "houston",
    "indiana",
    "detroit",
    "cleveland",
    "toronto",
    "atlanta",
    "baltimore",
    "pittsburgh",
    "tampa_bay",
    "denver",
    "seattle",
    "philadelphia",
    "minneapolis",
    "minnesota",
    "austin",
    "san_diego",
    "san_antonio",
    "miami",
    "columbus",
    "carolina",
    "phoenix",
    "dallas",
    "death_valley",
}

ENTITY_VOCAB: frozenset[str] = frozenset(ALIASES.values()) | frozenset(_EXTRA_ENTITIES)

# Longest-phrase-first so multi-word aliases (e.g. "united states") win over any
# single-word substring before word-boundary substitution.
_ALIAS_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in sorted(ALIASES, key=len, reverse=True)) + r")\b"
)


def _apply_aliases(text: str) -> str:
    """Fold known aliases onto their canonical, underscore-joined token."""
    return _ALIAS_PATTERN.sub(lambda m: ALIASES[m.group(0)], text)


def normalize_title(title: str) -> str:
    """Lowercase, fold aliases onto canonical tokens, and strip punctuation."""
    t = title.lower()
    t = _apply_aliases(t)
    t = re.sub(r"[^a-z0-9._]+", " ", t)  # keep '.' for '82.5'; '_' for canonical entities
    return re.sub(r"\s+", " ", t).strip()


def tokenize(title: str) -> set[str]:
    """Normalized, stopword-filtered token set used for similarity."""
    return {tok for tok in normalize_title(title).split() if tok and tok not in STOPWORDS}


def extract_entities(title: str) -> frozenset[str]:
    """Recognized entity tokens (countries/cities/crypto) mentioned in a title.

    Used to sub-block comparisons (see `resolve`): two markets are only compared
    if they share at least one extracted entity, OR neither has a recognized one.
    Unrecognized proper nouns (a team/ticker not in ENTITY_VOCAB) simply yield no
    entity for that title — a documented blocking trade-off, not a silent bug.
    """
    return frozenset(tokenize(title) & ENTITY_VOCAB)


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def numbers_in(title: str) -> frozenset[str]:
    """Distinguishing numbers in a title (thresholds, lines, strike values).

    Markets that differ in these numbers are NOT the same event — e.g. the
    temperature buckets "96F or higher" vs "97F or higher", or "wins by over 1.5"
    vs "over 2.5". Requiring the number sets to match prevents the fuzzy matcher
    from collapsing a whole ladder of near-identical-title markets into one event.
    """
    return frozenset(_NUM_RE.findall(title.lower()))


# Leading function words to skip before looking for a subject ("Will the X..."),
# and connector words that can appear INSIDE a multi-word proper noun without
# ending it ("Democratic Republic of Congo", "Dwayne 'The Rock' Johnson").
_SUBJECT_LEADING_SKIP = {"will", "the", "a", "an"}
_SUBJECT_CONNECTORS = {"of", "and", "the", "de", "la", "le", "del"}
_WORD_RE = re.compile(r"[\w']+", re.UNICODE)


def _starts_with_upper(word: str) -> bool:
    return any(ch.isupper() for ch in word[:1]) or (
        len(word) > 1 and word[0] in "'\"" and word[1:2].isupper()
    )


def subject_tokens(title: str) -> frozenset[str] | None:
    """The asserted subject: the capitalized proper-noun phrase the title is
    ABOUT, taken straight from the original (not yet lowercased) title text.

    "Boston Celtics beat the Los Angeles Lakers" -> {boston, celtics};
    "Los Angeles Lakers to win vs Boston Celtics" -> {los, angeles, lakers};
    "Will Dwayne 'The Rock' Johnson be the 2028 nominee?" -> {dwayne, rock, johnson};
    "Will Türkiye finish last in Group D...?" -> {türkiye}.

    This generalizes far better than a predicate-verb whitelist: a templated
    title repeats the same boilerplate predicate for many different subjects
    (candidates, countries, teams — anyone not in ENTITY_VOCAB), which fuzzy-matches
    on the shared boilerplate alone unless something differentiates the subject —
    a real over-merge bug found running this resolver against live World Cup and
    political-market data (see FINDINGS.md). Capitalization is a cheap, reliable
    signal for "this is the subject, not the predicate" without an NLP dependency.
    Returns None when the title doesn't open with a recognizable proper noun (no
    extra constraint — most titles, e.g. weather, aren't this template).
    """
    words = _WORD_RE.findall(title)
    i = 0
    while i < len(words) and words[i].lower() in _SUBJECT_LEADING_SKIP:
        i += 1
    if i >= len(words) or not _starts_with_upper(words[i]):
        return None
    captured = [words[i]]
    i += 1
    while i < len(words) and (
        _starts_with_upper(words[i]) or words[i].lower() in _SUBJECT_CONNECTORS
    ):
        captured.append(words[i])
        i += 1
    text = _apply_aliases(" ".join(captured).lower())
    subj = frozenset(tok for tok in re.split(r"[^\w]+", text) if tok and tok not in STOPWORDS)
    return subj or None


def guess_category(title: str) -> str:
    toks = set(normalize_title(title).split())
    for category, keywords in CATEGORY_KEYWORDS.items():
        if toks & keywords:
            return category
    return "other"


def title_similarity(tokens_a: set[str], tokens_b: set[str]) -> float:
    """Order-insensitive similarity in [0,1]: half token-Jaccard, half seq-ratio."""
    if not tokens_a or not tokens_b:
        return 0.0
    inter = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b) or 1
    jaccard = inter / union
    seq = difflib.SequenceMatcher(
        None, " ".join(sorted(tokens_a)), " ".join(sorted(tokens_b))
    ).ratio()
    return 0.5 * jaccard + 0.5 * seq


# --------------------------------------------------------------------------- #
# Optional pluggable layer: embedding-based candidate generation.
#
# Gated behind `resolve(use_embeddings=True)` — OFF by default, and never
# required: the std-lib token-Jaccard/sequence-ratio matcher above is the
# default and only matcher CI relies on. This layer's job is narrow — PROPOSE
# extra candidate pairs (e.g. across category/entity sub-blocks that share no
# tokens at all because the two platforms worded the title completely
# differently) for the SAME fuzzy scorer + guards to confirm or reject. It
# never matches anything by itself. Requires the optional `sentence-transformers`
# extra (`pip install edgeradar[embeddings]`); if it isn't installed, it no-ops
# with a one-line notice rather than failing, so the offline demo and CI never
# need a model download.
# --------------------------------------------------------------------------- #


def _embedding_model(model_name: str):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return None
    return SentenceTransformer(model_name)


def embedding_candidate_pairs(
    records: list[dict],
    *,
    model_name: str = "all-MiniLM-L6-v2",
    top_k: int = 5,
    min_similarity: float = 0.55,
) -> list[tuple[tuple[str, str], tuple[str, str], float]]:
    """Propose (key_a, key_b, similarity) candidate pairs via title embeddings.

    Returns an empty list (with a printed notice) when `sentence-transformers`
    isn't installed — fail-soft by design, never an import error the caller has
    to handle. Each title's `top_k` nearest neighbors by cosine similarity above
    `min_similarity` are proposed; the caller (`resolve`) still runs every
    proposal through the normal numeric/subject guards and threshold before
    treating it as a match.
    """
    model = _embedding_model(model_name)
    if model is None:
        print(
            "[entity_resolution] sentence-transformers not installed; skipping "
            "embedding-based candidate generation (std-lib matcher only). "
            "Install with: pip install edgeradar[embeddings]"
        )
        return []
    if not records:
        return []

    titles = [r["title"] for r in records]
    keys = [(r["source"], r["market_id"]) for r in records]
    embeddings = model.encode(titles, normalize_embeddings=True)
    sims = embeddings @ embeddings.T

    pairs: list[tuple[tuple[str, str], tuple[str, str], float]] = []
    seen: set[tuple[tuple[str, str], tuple[str, str]]] = set()
    n = len(records)
    for i in range(n):
        ranked = sorted(range(n), key=lambda j: -sims[i, j])
        taken = 0
        for j in ranked:
            if j == i:
                continue
            score = float(sims[i, j])
            if score < min_similarity:
                break
            ordered = tuple(sorted([keys[i], keys[j]]))
            if ordered not in seen:
                seen.add(ordered)
                pairs.append((ordered[0], ordered[1], score))
            taken += 1
            if taken >= top_k:
                break
    return pairs


def _date_bonus(a: pd.Timestamp | None, b: pd.Timestamp | None) -> float:
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return 0.0
    days = abs((a.date() - b.date()).days)
    if days <= 1:
        return 0.10
    if days <= 3:
        return 0.05
    return 0.0


def _event_id(members: Iterable[tuple[str, str]]) -> str:
    """Stable short id from the sorted set of (source, market_id) members."""
    joined = "|".join(sorted(f"{s}:{m}" for s, m in members))
    return "evt_" + hashlib.md5(joined.encode()).hexdigest()[:10]


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[tuple[str, str], tuple[str, str]] = {}

    def find(self, x: tuple[str, str]) -> tuple[str, str]:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: tuple[str, str], b: tuple[str, str]) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


@dataclass
class ResolveResult:
    event_map: pd.DataFrame  # one row per market with its event_id
    candidate_pairs: pd.DataFrame  # scored pairs (matches + review band)
    event_map_path: str | None = None
    n_events: int = 0
    n_cross_platform: int = 0
    overrides_applied: int = 0
    flagged_for_review: list[dict] = field(default_factory=list)
    diagnostics: ResolutionDiagnostics | None = None


def _load_override_pairs(path: str | Path) -> list[tuple[tuple[str, str], tuple[str, str], str]]:
    """Read the manual override table. Rows referencing unknown markets are inert."""
    p = Path(path)
    if not p.exists():
        return []
    df = pd.read_csv(p, dtype=str).fillna("")
    pairs = []
    for _, r in df.iterrows():
        a = (r["source_a"].strip(), r["market_id_a"].strip())
        b = (r["source_b"].strip(), r["market_id_b"].strip())
        relation = r["relation"].strip().lower()
        if a[0] and a[1] and b[0] and b[1] and relation in {"match", "block"}:
            pairs.append((a, b, relation))
    return pairs


def load_latest_markets(*, data_root: str | None = None) -> pd.DataFrame:
    """One row per (source, market_id): its most recent title + close date."""
    df = read_quotes(data_root=data_root)
    if df.empty:
        return df
    df = df.sort_values("snapshot_ts").groupby(["source", "market_id"], as_index=False).last()
    df = df[df["title"].astype(str).str.len() > 0].copy()
    df["category"] = df["title"].map(guess_category)
    df["tokens"] = df["title"].map(tokenize)
    df["numbers"] = df["title"].map(numbers_in)
    df["subject"] = df["title"].map(subject_tokens)
    df["entities"] = df["title"].map(extract_entities)
    df["close_ts"] = pd.to_datetime(df["close_ts"], utc=True, errors="coerce")
    return df


def resolve(
    *,
    data_root: str | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    overrides_path: str | None = None,
    write: bool = True,
    use_embeddings: bool = False,
) -> ResolveResult:
    """Run entity resolution over the landed markets and (optionally) write event_map.

    `use_embeddings` is OFF by default (and what CI runs): the std-lib fuzzy
    matcher above is the only matcher required. When True, it additionally asks
    `embedding_candidate_pairs` to propose extra pairs — e.g. across category or
    entity sub-blocks that share no tokens at all because the platforms worded
    the same event completely differently — for the SAME guards/threshold to
    confirm. Requires the optional `sentence-transformers` extra; if missing,
    this is a silent (logged) no-op, never a hard failure.
    """
    settings = get_settings()
    root = data_root or settings.data_root
    overrides_path = overrides_path or "seeds/event_overrides.csv"

    markets = load_latest_markets(data_root=root)
    if markets.empty:
        diag = compute_resolution_diagnostics(markets, pd.DataFrame(), n_cross_platform=0)
        if write:
            write_resolution_diagnostics(diag, data_root=root)
        return ResolveResult(
            event_map=pd.DataFrame(), candidate_pairs=pd.DataFrame(), diagnostics=diag
        )

    records = markets.to_dict("records")
    key = lambda r: (r["source"], r["market_id"])  # noqa: E731

    overrides = _load_override_pairs(overrides_path)
    known = {key(r) for r in records}
    block_set = {
        tuple(sorted([a, b]))
        for a, b, rel in overrides
        if rel == "block" and a in known and b in known
    }
    match_set = {
        tuple(sorted([a, b]))
        for a, b, rel in overrides
        if rel == "match" and a in known and b in known
    }

    # --- score candidate pairs within each (category, entity) sub-block --------
    # Sub-blocking by extracted entity (on top of the coarse category) keeps
    # comparisons cheap AND precise: two markets are only fuzzy-compared if they
    # share a recognized entity (country/city/ticker), or neither has one. This is
    # what stops near-identical templated titles for DIFFERENT entities (e.g. "Will
    # Bosnia win Group F?" vs "Will Argentina win Group J?" — high token overlap on
    # boilerplate words alone) from ever being scored against each other, which in
    # turn stops them from being transitively chained into one false giant event —
    # a real over-merge found by running this resolver against live World Cup data
    # (see FINDINGS.md). Records with no recognized entity still share one
    # catch-all sub-block per category (a documented blocking trade-off).
    pairs: list[dict] = []
    uf = _UnionFind()
    rec_by_key: dict[tuple[str, str], dict] = {}
    for r in records:
        uf.find(key(r))  # ensure every market is a node (singletons included)
        rec_by_key[key(r)] = r

    by_subblock: dict[tuple[str, frozenset[str]], list[dict]] = {}
    for r in records:
        by_subblock.setdefault((r["category"], r["entities"]), []).append(r)

    def _score_pair(a: dict, b: dict) -> tuple[float, float, float, bool, bool, float]:
        """sim, bonus, confidence, nums_ok, subject_ok, required_threshold."""
        sim = title_similarity(a["tokens"], b["tokens"])
        bonus = _date_bonus(a["close_ts"], b["close_ts"])
        confidence = min(1.0, sim + bonus)
        # Distinct thresholds/lines (different numbers) => different events, even if
        # the titles are otherwise near-identical. Only block when BOTH titles carry
        # numbers and they differ (so a 96F vs 97F ladder is split), but allow
        # matches when one side has no number (e.g. a sportsbook title without a
        # date) so legitimate cross-platform pairs aren't over-blocked.
        na, nb = a["numbers"], b["numbers"]
        nums_ok = (not na) or (not nb) or (na == nb)
        # Opposite sides of the same game ("Celtics win" vs "Lakers win") have
        # near-identical titles but complementary probabilities — never merge them.
        # Require the asserted winner (subject) to overlap.
        sa, sb = a["subject"], b["subject"]
        subject_ok = (sa is None) or (sb is None) or bool(sa & sb)
        # A title with NEITHER a recognized entity NOR a capitalized subject is
        # maximally generic ("Will every team score a goal...?") and both the
        # entity sub-block and the subject guard are vacuous for it — it would
        # otherwise act as a promiscuous "bridge" that transitively unions two
        # genuinely different, specific events through itself (a real over-merge
        # bug found on live World Cup data: dozens of unrelated countries'
        # markets collapsing into one event via such a bridge — see FINDINGS.md).
        # Demand near-exact similarity before letting a generic title match.
        generic = (not a["entities"] and sa is None) or (not b["entities"] and sb is None)
        required = GENERIC_MATCH_THRESHOLD if generic else threshold
        return sim, bonus, confidence, nums_ok, subject_ok, required

    overrides_applied = 0
    scored_pairs: set[tuple[tuple[str, str], tuple[str, str]]] = set()

    for cat, group in by_subblock.items():
        category = cat[0]
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                ka, kb = key(a), key(b)
                ordered = tuple(sorted([ka, kb]))
                scored_pairs.add(ordered)

                sim, bonus, confidence, nums_ok, subject_ok, required = _score_pair(a, b)
                method = "fuzzy"
                decision = (
                    "match" if (confidence >= required and nums_ok and subject_ok) else "no-match"
                )

                if ordered in block_set:
                    decision, method, overrides_applied = (
                        "manual-block",
                        "manual",
                        overrides_applied + 1,
                    )
                elif ordered in match_set:
                    confidence, decision, method = 1.0, "manual-match", "manual"
                    overrides_applied += 1

                if decision in {"match", "manual-match"}:
                    uf.union(ka, kb)

                if (
                    decision in {"match", "manual-match", "manual-block"}
                    or confidence >= threshold - REVIEW_BAND
                ):
                    pairs.append(
                        {
                            "source_a": ka[0],
                            "market_id_a": ka[1],
                            "title_a": a["title"],
                            "source_b": kb[0],
                            "market_id_b": kb[1],
                            "title_b": b["title"],
                            "category": category,
                            "similarity": round(sim, 4),
                            "date_bonus": bonus,
                            "confidence": round(confidence, 4),
                            "method": method,
                            "decision": decision,
                        }
                    )

    # A manual override always wins, even for a pair that sub-blocking never would
    # have compared (e.g. an entity our gazetteer doesn't recognize) — overrides are
    # a human decision, not subject to the automatic blocking trade-off.
    for a_key, b_key, relation in overrides:
        ordered = tuple(sorted([a_key, b_key]))
        if ordered in scored_pairs or a_key not in rec_by_key or b_key not in rec_by_key:
            continue
        a, b = rec_by_key[a_key], rec_by_key[b_key]
        sim, bonus, confidence, _, _, _ = _score_pair(a, b)
        if relation == "block":
            decision, method = "manual-block", "manual"
        else:
            confidence, decision, method = 1.0, "manual-match", "manual"
            uf.union(a_key, b_key)
        overrides_applied += 1
        pairs.append(
            {
                "source_a": a_key[0],
                "market_id_a": a_key[1],
                "title_a": a["title"],
                "source_b": b_key[0],
                "market_id_b": b_key[1],
                "title_b": b["title"],
                "category": a["category"],
                "similarity": round(sim, 4),
                "date_bonus": bonus,
                "confidence": round(confidence, 4),
                "method": method,
                "decision": decision,
            }
        )

    # Optional embedding layer: propose extra candidate pairs (e.g. across
    # sub-blocks that share no tokens at all) for the SAME guards/threshold to
    # confirm. Never bypasses nums_ok/subject_ok/the match threshold — it only
    # widens which pairs get a chance to be scored.
    if use_embeddings:
        for a_key, b_key, _emb_score in embedding_candidate_pairs(records):
            ordered = tuple(sorted([a_key, b_key]))
            if ordered in scored_pairs or ordered in block_set:
                continue
            scored_pairs.add(ordered)
            a, b = rec_by_key[a_key], rec_by_key[b_key]
            sim, bonus, confidence, nums_ok, subject_ok, required = _score_pair(a, b)
            ok = confidence >= required and nums_ok and subject_ok
            decision = "match" if ok else "no-match"
            if decision == "match":
                uf.union(a_key, b_key)
            if decision == "match" or confidence >= threshold - REVIEW_BAND:
                pairs.append(
                    {
                        "source_a": a_key[0],
                        "market_id_a": a_key[1],
                        "title_a": a["title"],
                        "source_b": b_key[0],
                        "market_id_b": b_key[1],
                        "title_b": b["title"],
                        "category": a["category"],
                        "similarity": round(sim, 4),
                        "date_bonus": bonus,
                        "confidence": round(confidence, 4),
                        "method": "embedding",
                        "decision": decision,
                    }
                )

    # --- assign event ids from clusters --------------------------------------
    clusters: dict[tuple[str, str], list[tuple[str, str]]] = {}
    for r in records:
        clusters.setdefault(uf.find(key(r)), []).append(key(r))

    member_to_event: dict[tuple[str, str], str] = {}
    canonical: dict[str, str] = {}
    for members in clusters.values():
        eid = _event_id(members)
        for m in members:
            member_to_event[m] = eid
        # canonical title = the longest member title (usually the most descriptive)
        titles = [r["title"] for r in records if key(r) in set(members)]
        canonical[eid] = max(titles, key=len)

    # confidence each market joined by = best accepted edge incident to it
    joined_conf: dict[tuple[str, str], float] = {}
    joined_method: dict[tuple[str, str], str] = {}
    for p in pairs:
        if p["decision"] in {"match", "manual-match"}:
            for k in [(p["source_a"], p["market_id_a"]), (p["source_b"], p["market_id_b"])]:
                if p["confidence"] >= joined_conf.get(k, -1):
                    joined_conf[k] = p["confidence"]
                    joined_method[k] = p["method"]

    rows = []
    for r in records:
        k = key(r)
        eid = member_to_event[k]
        cluster_size = len(clusters[uf.find(k)])
        rows.append(
            {
                "source": k[0],
                "market_id": k[1],
                "title": r["title"],
                "category": r["category"],
                "event_id": eid,
                "canonical_title": canonical[eid],
                "match_method": joined_method.get(k, "fuzzy" if cluster_size > 1 else "singleton"),
                "match_confidence": joined_conf.get(k) if cluster_size > 1 else None,
            }
        )
    event_map = pd.DataFrame(rows)

    sizes = event_map.groupby("event_id")["source"].nunique()
    n_cross = int((sizes > 1).sum())

    candidate_pairs = (
        pd.DataFrame(pairs).sort_values("confidence", ascending=False) if pairs else pd.DataFrame()
    )
    flagged = (
        candidate_pairs[candidate_pairs["decision"] == "no-match"].to_dict("records")
        if not candidate_pairs.empty
        else []
    )

    diag = compute_resolution_diagnostics(markets, candidate_pairs, n_cross_platform=n_cross)

    out_path = None
    if write:
        out_dir = Path(root) / "marts"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / "event_map.parquet")
        event_map.to_parquet(out_path, index=False)
        # Persist the scored candidate pairs too, so the dashboard's resolution
        # workbench can show accepted matches AND near-miss reviews without rerunning.
        if not candidate_pairs.empty:
            candidate_pairs.to_parquet(out_dir / "candidate_pairs.parquet", index=False)
        write_resolution_diagnostics(diag, data_root=root)

    return ResolveResult(
        event_map=event_map,
        candidate_pairs=candidate_pairs,
        event_map_path=out_path,
        n_events=int(event_map["event_id"].nunique()),
        n_cross_platform=n_cross,
        overrides_applied=overrides_applied,
        flagged_for_review=flagged,
        diagnostics=diag,
    )
