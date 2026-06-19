# EdgeRadar — Architecture & Design Decisions

This document records *why* EdgeRadar is built the way it is. It's written for a
reviewer (or future me) trying to understand the trade-offs, not just the wiring.

## 1. Why read-only (the most important decision)

EdgeRadar is a **measurement and alerting** system. It never places orders or
executes trades. Two reasons:

1. **Safety.** These are real-money markets with real risk of loss. An automated
   executor that acts on an unproven signal can lose money fast. A human in the
   loop is the cheapest, most effective risk control.
2. **Honesty.** The whole point is to find out whether a measured "edge" is real.
   You can't know that until signals have been logged at quote time and scored
   against actual outcomes across *many* events (Phase 6). Acting before that is
   guessing, not edge.

This is enforced in code: `Settings.enable_order_execution` defaults to `False`,
the CLI refuses to proceed if it's flipped on, and there is simply no order-placement
code path. If a future request asks for auto-execution, it should be refused with a
pointer back to this section.

## 2. The source-adapter abstraction

Every platform is wrapped by a subclass of `SourceAdapter` (`adapters/base.py`)
exposing `fetch()` → raw payloads and `normalize()` → `MarketQuote` records, tied
together by `run()`. The rest of the pipeline depends only on this interface and
the `MarketQuote` contract.

Why it matters: adding a new platform becomes a self-contained task — implement one
class, drop in a sample response for `--dry-run`, write one test. Nothing else in
the system needs to change. This is the seam that keeps a multi-source ingestion
system from turning into spaghetti.

**Live-data gotcha (Kalshi).** A naive `fetch()` calling `/markets?status=open` got
0 normal markets out of 200 — Kalshi's default feed order is dominated >100:1 by
illiquid MVE combo/parlay baskets that report `market_type: "binary"` identically
to a real market (see `FINDINGS.md`). `adapters/kalshi.py` excludes them by the
real signal (`mve_collection_ticker` / `mve_selected_legs` / a `KXMVE...` ticker
prefix), paginates with a configurable page cap, and — since blind pagination
alone is impractical at that skew — supports `series_ticker`-targeted fetches
wired from `targeting.py` (`edgeradar ingest --categories ...`) as the practical
way to find real, overlap-worthy markets. When a quote falls back to a recent
last-traded price instead of a live two-sided quote, it's carried through as
`price_is_stale=True` end-to-end (model → lake → dbt → `quality.py`) — never
silently.

Each adapter also documents its **price → implied probability** formula, because
every platform quotes differently:

- **American odds** (`+150`, `-200`): `+`→ `100/(odds+100)`, `−`→ `−odds/(−odds+100)`.
- **Decimal odds** (`2.50`): `1/decimal`.
- **Kalshi cents** (1–99¢): contracts settle at $1, so YES price in dollars *is* the probability.
- **Polymarket shares** (0–1): shares pay $1, so share price *is* the probability.

## 3. Implied probability, vig, and fee adjustment

A market price is a bet, so it encodes the market's probability estimate. To compare
platforms we convert all prices to a probability in `(0,1)`.

**Vig / overround:** two-sided books price both sides to sum to *more* than 1; the
excess is the house margin. We remove it (Phase 1: proportional normalization —
divide each side by the sum) so a sportsbook is comparable to a fairer market.

**Fee/spread adjustment (implemented, Phase 5):** a divergence only matters if it
survives the cost to act. Rather than fold the cost into a single `fee_adj_prob`
scalar (whose sign would depend on which side you take), each quote carries an
explicit `trade_cost` in probability units = **half the bid/ask spread + the Kalshi
fee** `0.07·P·(1−P)` (largest near P=0.5). Manifold is play money, so its cost is 0
— a consensus reference, not something you'd trade. The divergence engine reports
`edge_net = |deviation| − trade_cost` and only flags `is_signal` when that's
positive; on the sample data a 0.030 raw Kalshi divergence becomes a ~0.014 net edge
after its ~0.016 cost. `fee_adj_prob` is retained as the fair point estimate (the mid).

**Weather edge (implemented, Phase 5):** NWS publishes the official forecast high.
We treat a point forecast `H` as the mean of a Normal(`H`, sigma) and read
`P(high > threshold) = 1 − Phi((threshold − H)/sigma)`, parse the threshold out of
the Kalshi market title, and report the edge net of trading cost. The Normal/sigma
assumption is deliberately simple — Phase 6 scores whether these forecast-implied
probabilities are actually calibrated against outcomes.

## 4. Why entity resolution is hard (Phase 4)

To compare platforms on "the same event," we must first decide that, say, a Kalshi
market and a Manifold market *are* the same event. This is genuinely hard:

- Titles differ ("Will the Lakers win?" vs "Lakers to beat Celtics 2026-01-02").
- Granularity differs (a single game vs a series; "highest temp" vs "temp ≥ 90°F").
- Outcome spaces differ (binary vs multi-outcome vs scalar/temperature buckets).
- Resolution criteria and close times differ even for nominally identical events.

The plan is a **layered matcher**: exact/rule-based joins where keys exist (e.g.
shared game IDs, dates + team names), fuzzy matching on titles/dates/categories with
a **confidence score**, and a **manual override mapping table** for ambiguous cases
that a human confirms once. An LLM-assisted matcher is a natural later upgrade for
the fuzzy tier (embed titles, propose candidate pairs for human review) — noted but
not required. Every match carries its confidence so low-confidence groupings are
reviewable rather than silently trusted.

**Implemented (Phase 4)** in `entity_resolution.py`: features = normalized token
set + coarse category + close date; **blocking** by category to avoid an O(n^2)
sweep; fuzzy score = ½ token-set Jaccard + ½ sequence ratio, plus a small same-date
bonus; a `seeds/event_overrides.csv` table whose `match`/`block` rows override the
fuzzy score; union-find clustering into `event_id`s. `make resolve` prints proposed
and near-threshold pairs for human review. The choice of std-lib-only matching (no
heavyweight similarity dependency) keeps the logic transparent and auditable; the
override table is the seam where an LLM matcher's confirmations would later land.

**Hardened against live-scale data (Task 3 follow-up).** Running resolution against
a few hand-written fixtures never exercises the failure mode that running it against
~3,500 real, differently-worded markets does: a pairwise guard can be defeated by
*transitive* clustering through a third "bridge" market, even when the two true
endpoints would never score high against each other directly. Concretely added:

- **Alias normalization** (`ALIASES`): folds known synonyms (USA/United States,
  BTC/Bitcoin, NYC/New York) onto one canonical, underscore-joined token *before*
  tokenizing, so `title_similarity` sees them as identical, not merely similar.
- **Entity sub-blocking**: blocks by `(category, extracted_entity)`, not just
  category, using the same canonical tokens as a small gazetteer. Different
  countries/cities/tickers are never even compared — this is what stops a
  templated "Will X win Group Y?" ladder for *different* X from fuzzy-matching on
  shared boilerplate alone.
- **Subject guard, generalized**: the old win/lose-word heuristic only fired on
  "beat"/"win"; it's now capitalization-based proper-noun extraction (works for
  any "Will X verb...?" template — candidates, countries, anyone), and tightened
  from "any shared token" to "one subject is a SUBSET of the other" (still allows
  an abbreviated mention, but blocks "Messi vs Ronaldo" matching unrelated
  "Messi wins the Golden Ball" on the shared token "messi" alone).
- **Predicate guard**: the same subject (e.g. one country) is the subject of many
  genuinely different propositions ("win the cup" / "win their group" / "reach the
  quarterfinals" / "go unbeaten") sharing heavy boilerplate. A small predicate
  keyword set (win/reach/group/stage/unbeaten/concede/cut/hike/...) must match
  exactly between two titles that both have one.
- **Generic-title threshold**: a title with neither a recognized entity nor a
  subject is maximally generic and would otherwise act as a promiscuous bridge;
  it now needs near-exact similarity (0.92, vs the normal 0.60) to match anything.
- **Optional embedding-candidate layer** (`embedding_candidate_pairs`, gated behind
  `resolve(use_embeddings=True)` and the `edgeradar[embeddings]` extra): proposes
  extra candidate pairs across blocks that share no tokens at all, for the SAME
  guards above to confirm or reject. The std-lib matcher remains the default and
  the only one CI exercises — this is the seam the original "LLM-assisted matcher"
  idea above landed in.

Net effect on live data: max false-cluster size 304 → 5; cross-platform
`mart_divergence` rows 481 → 99 (the inflation was mostly false matches comparing
different propositions). See `FINDINGS.md` for the full live-data writeup,
including the one residual gap left undefended (month-name differentiation).

**Resolution diagnostics.** `resolution_diagnostics.py` turns the same
intermediate data `resolve()` already computes (per-market category/entity,
scored candidate pairs) into a persisted report: counts per source per category,
near-miss score distribution, and a plain-language explanation for *why* zero
cross-platform events matched, when that happens — surfaced on the dashboard's
Resolution page instead of a bare empty state.

## 5. Storage & warehouse choices

- **MinIO (S3-compatible)** as the data lake: local, free, and identical API to S3
  if this ever moves to the cloud. Data lands as **Parquet partitioned by
  `source/date`** — columnar, compressed, and cheap to scan by source/day.
- **Raw zone + clean zone.** We persist raw payloads verbatim before normalizing, so
  if our math changes we can re-derive without re-hitting (rate-limited) APIs, and so
  every signal stays auditable back to its source bytes.
- **DuckDB** as the warehouse to start: an embedded, file-based OLAP engine — zero
  infra, fast local analytics, and SQL that ports cleanly. The schema and dbt models
  are written so a later swap to **Postgres** is mostly a connection change.

## 6. Idempotency & time

- **Natural key:** `(source, market_id, outcome, snapshot_ts)`. Re-running ingestion
  for the same snapshot must never create duplicates — upserts/merges key on this.
- **All timestamps are UTC**, timezone-aware. A quote also carries (where known) a
  validity window (`valid_from`) and the market `close_ts`, so we never compare a
  stale quote to a fresh one as if simultaneous.

## 7. Streaming (Phase 3) — why Redpanda

Phase 1 lands data directly (adapter → Parquet) to keep early phases simple. Phase 3
inserts **Redpanda** (a single-binary, Kafka-compatible broker — no ZooKeeper) so
producers (adapters) and the normalizing consumer are decoupled. This models a real
streaming pipeline (the interview talking point) while staying to one container.

## 8. Evaluation is the credibility core (Phase 6, implemented)

Every flagged signal is appended to `signal_log` (`evaluation.py`) with the prices
and implied/forecast probabilities *at signal time*, keyed so reruns don't
double-count. Known outcomes come from `seeds/resolutions.csv` (live: Kalshi's
settled `result` and NWS observed highs). Scoring computes, per signal: the implied
side, whether it won (`hit`), the model's predicted probability for that side
(calibration), and per-contract **PnL net of fees** — summed only over the
*tradeable* (Kalshi) side, because Manifold is play money and must never inflate a
"profit" number. A **Dagster** asset graph (`signal_log → scored_signals →
eval_dbt_models`) orchestrates this, and dbt eval marts (`mart_signal_scores`,
`mart_calibration`) present it.

A pretty dashboard proves nothing; the calibration report is the evidence. The PnL
accounting is intentionally conservative (real cost, tradeable side only) so the
system can't flatter itself. No acting on signals until this shows a real,
fee-surviving edge across **many** events — a few samples are illustrative only.

## 9. Serving + alerts (Phase 7, implemented)

The **Streamlit dashboard** (`dashboard/app.py`) is a thin, read-only, 7-page
product over the DuckDB marts and the Parquet reports: Overview (KPIs),
Divergences (filterable explorer with per-signal edge breakdowns), Resolution
(matched events + near misses), Weather, Calibration, Source health, and System
status. It opens DuckDB in read-only mode, caches queries briefly, and degrades to
a friendly empty state (never a raw error) when a stage hasn't run. The **Discord
alerter** (`alerter.py`) selects signals above `alert_min_edge`, formats a short
digest, and POSTs it to a webhook (or prints with `--dry-run`). Both assert the
read-only guardrail; the alerter raises rather than run if `enable_order_execution`
is ever true. Neither has any code path that could place an order — they only
surface information to a human.

## 10. The explainable analytics layer

`analytics.py` is a set of **pure functions** — no model state, no network — so
every label the dashboard shows can be explained in one sentence. It decomposes an
apparent edge into `raw → fee_adjusted → uncertainty_adjusted` (the last layer
subtracts cross-platform price dispersion, charging the edge for a noisy
consensus); assigns a **confidence tier** from venue count and agreement; and
scores **source reliability** (0–100) by blending freshness, completeness, and —
when outcomes exist — calibration, redistributing weight rather than penalizing a
source that simply has no settled history yet. The confidence logic is duplicated
intentionally in both Python and `mart_divergence.sql` and kept byte-for-byte in
sync so the warehouse and the dashboard can never disagree; a test asserts the
Python side matches the SQL thresholds.

The dbt staging models are collapsed onto a single `stage_quotes(source)` **macro**
so all four sources share one definition — the union in `fact_market_quotes` can
never drift because there is only one piece of logic to change.

## 11. Data quality & observability

`quality.py` turns the lake into a health report (`data/quality/data_quality.parquet`,
one row per source) using deliberately boring, high-value checks: freshness,
volume, null rate, **duplicate rate** (a direct test of ingestion idempotency —
the natural key should make it zero), probability-bounds violations, a
partial-ingest heuristic (latest snapshot vs the source's median volume),
**stale-price-fallback rate** (quotes whose `implied_prob` came from a flagged
last-traded-price fallback rather than a live two-sided quote), and **combo-market
exclusion rate** (the fraction of RAW Kalshi payloads dropped as MVE combo/parlay
baskets before normalizing — the original live-data gotcha this report exists to
catch). `_discover_sources` includes raw-only sources too, so a source whose
*every* fetched record got excluded (0 clean quotes) still shows up as
"100% combo-excluded, grade F" instead of silently vanishing from the report. The
checks are pure functions over a DataFrame, unit-tested independently of the lake,
and surfaced both in the dashboard's Source-health page and via `edgeradar
quality`. `edgeradar doctor` complements this by diagnosing the *environment*
(Python, deps, files, sample data, the read-only guardrail, current Kalshi
ingestion knobs, and whether the optional embeddings extra is installed) before
a demo.

## 12. Open questions / decisions to revisit

- Consensus weighting: equal vs volume-weighted vs reliability-weighted (the
  reliability score in `analytics.py` is the natural input for the last option).
- Whether to promote DuckDB → Postgres (the dbt models are written to make this a
  connection change).
- Longer-horizon live signal calibration as logged signals accumulate resolutions.
- Normalizing month-name abbreviations ("Jun" vs "June") in the entity-resolution
  predicate guard — the one residual over-merge case found and left unfixed; see
  `FINDINGS.md`.
