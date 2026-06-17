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

# Coarse category keyword buckets. First bucket with a hit wins.
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
        "game",
        "vs",
        "celtics",
        "lakers",
        "nba",
        "nfl",
        "mlb",
        "nhl",
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
    },
    "econ": {
        "retail",
        "sales",
        "cpi",
        "inflation",
        "gdp",
        "rate",
        "fed",
        "unemployment",
        "earnings",
        "jobs",
    },
    "crypto": {"bitcoin", "btc", "ethereum", "eth", "crypto", "solana"},
}

DEFAULT_THRESHOLD = 0.60  # confidence at/above which a fuzzy pair is accepted
REVIEW_BAND = 0.12  # pairs within this much below threshold are flagged for review


def normalize_title(title: str) -> str:
    """Lowercase and strip punctuation to a clean, space-separated string."""
    t = title.lower()
    t = re.sub(r"[^a-z0-9.]+", " ", t)  # keep '.' so '82.5' survives
    return re.sub(r"\s+", " ", t).strip()


def tokenize(title: str) -> set[str]:
    """Normalized, stopword-filtered token set used for similarity."""
    return {tok for tok in normalize_title(title).split() if tok and tok not in STOPWORDS}


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def numbers_in(title: str) -> frozenset[str]:
    """Distinguishing numbers in a title (thresholds, lines, strike values).

    Markets that differ in these numbers are NOT the same event — e.g. the
    temperature buckets "96F or higher" vs "97F or higher", or "wins by over 1.5"
    vs "over 2.5". Requiring the number sets to match prevents the fuzzy matcher
    from collapsing a whole ladder of near-identical-title markets into one event.
    """
    return frozenset(_NUM_RE.findall(title.lower()))


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
    df["close_ts"] = pd.to_datetime(df["close_ts"], utc=True, errors="coerce")
    return df


def resolve(
    *,
    data_root: str | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    overrides_path: str | None = None,
    write: bool = True,
) -> ResolveResult:
    """Run entity resolution over the landed markets and (optionally) write event_map."""
    settings = get_settings()
    root = data_root or settings.data_root
    overrides_path = overrides_path or "seeds/event_overrides.csv"

    markets = load_latest_markets(data_root=root)
    if markets.empty:
        return ResolveResult(event_map=pd.DataFrame(), candidate_pairs=pd.DataFrame())

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

    # --- score candidate pairs within each category block ---------------------
    pairs: list[dict] = []
    uf = _UnionFind()
    for r in records:
        uf.find(key(r))  # ensure every market is a node (singletons included)

    by_cat: dict[str, list[dict]] = {}
    for r in records:
        by_cat.setdefault(r["category"], []).append(r)

    overrides_applied = 0
    for cat, group in by_cat.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                ka, kb = key(a), key(b)
                ordered = tuple(sorted([ka, kb]))

                sim = title_similarity(a["tokens"], b["tokens"])
                bonus = _date_bonus(a["close_ts"], b["close_ts"])
                confidence = min(1.0, sim + bonus)
                method = "fuzzy"
                # Distinct thresholds/lines (different numbers) => different events,
                # even if the titles are otherwise near-identical.
                nums_match = a["numbers"] == b["numbers"]
                decision = "match" if (confidence >= threshold and nums_match) else "no-match"

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
                            "category": cat,
                            "similarity": round(sim, 4),
                            "date_bonus": bonus,
                            "confidence": round(confidence, 4),
                            "method": method,
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

    out_path = None
    if write:
        out_dir = Path(root) / "marts"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / "event_map.parquet")
        event_map.to_parquet(out_path, index=False)

    return ResolveResult(
        event_map=event_map,
        candidate_pairs=candidate_pairs,
        event_map_path=out_path,
        n_events=int(event_map["event_id"].nunique()),
        n_cross_platform=n_cross,
        overrides_applied=overrides_applied,
        flagged_for_review=flagged,
    )
