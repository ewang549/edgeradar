# EdgeRadar

![CI](https://github.com/ethanwang/edgeradar/actions/workflows/ci.yml/badge.svg)

**EdgeRadar is a read-only data platform that watches the same real-world events
across multiple prediction markets and sportsbooks, and surfaces where one venue
disagrees with the consensus — after fees, after uncertainty, and only when the
data underneath is trustworthy.** It ingests live odds, normalizes everything to
implied probabilities, figures out which differently-worded markets describe the
same event, ranks the divergences, and then does the part most "alpha-finder"
projects skip: it scores its own predictions against settled outcomes so you can
tell whether an apparent edge is real.

> 🔒 **EdgeRadar never places orders, never executes trades, and contains no
> betting flow.** It ingests public data and presents it to a human for review.
> Every signal is logged and later scored against the real outcome. The honest
> conclusion baked into the project: prediction markets are mostly efficient and
> trading costs usually eat the gap — so the deliverable is a rigorous analytics
> and evaluation system, not a money printer. See [`FINDINGS.md`](FINDINGS.md).

---

## Why this is technically interesting

It is a complete, production-shaped data system rather than a single script:

- **Streaming ingestion** through a Kafka-compatible broker (Redpanda) with a
  pluggable adapter per source — adding a venue is one class.
- **A partitioned Parquet lake → DuckDB warehouse → dbt** with staging/marts and
  data-quality tests, plus a DRY macro so all four source-staging models stay in
  lockstep.
- **Entity resolution** that matches the *same event* across venues using alias
  normalization (USA/United States, BTC/Bitcoin), category + entity sub-blocking,
  fuzzy title+date scoring, numeric/subject/predicate guards, an optional
  pluggable embedding-candidate layer, and a human override table — the genuinely
  hard part. A persisted diagnostics report explains *why* when nothing matches
  (e.g. "no category had ≥2 sources") instead of a bare empty state.
- **An explainable analytics layer** that decomposes every apparent edge into
  raw → fee-adjusted → uncertainty-adjusted, attaches a confidence tier, and
  scores each source's reliability. No black-box ML; every label is a one-line
  explanation.
- **Forecast evaluation done honestly**: signal logging, calibration buckets,
  Brier scores, and a market-wide calibration study that surfaced a real
  favorite-longshot bias across 1,500+ settled markets.
- **Observability**: a data-quality report (freshness, null/duplicate rates,
  probability-bounds checks, partial-ingest detection) that the dashboard renders
  as source-health grades.
- **Operational polish**: a 7-page Streamlit product, Dagster orchestration,
  Docker Compose for the whole stack, GitHub Actions CI, and a one-command
  offline demo.

## Quick start — the 60-second offline demo

Everything runs locally and free. The fastest path uses bundled sample responses,
so it needs no API keys, no network, and no live markets:

```bash
cp .env.example .env     # defaults are fine
make up                  # build + start the stack (Docker Desktop required)
make doctor              # sanity-check the environment
make demo                # offline: ingest → resolve → warehouse → quality report
make dashboard           # open http://localhost:8501
```

`make demo` builds the lake from `sample_responses/`, runs entity resolution,
builds the dbt warehouse, and writes the data-quality report — populating every
page of the dashboard deterministically.

To run against **live** public data instead (Manifold and Kalshi reads need no
key; The Odds API needs a free key in `.env`):

```bash
make refresh             # pull live data, rebuild warehouse, score, quality report
make notify              # same, then post above-threshold signals to Discord
```

Kalshi's default `/markets` feed is dominated by illiquid MVE combo/parlay baskets
(see [`FINDINGS.md`](FINDINGS.md)), so a "pull everything" live run finds few
real, overlap-worthy markets. For a live run that actually surfaces cross-platform
matches, target the categories known to co-exist across platforms:

```bash
edgeradar ingest --source all --categories world_cup,crypto,elections,macro,weather,sports_finals
```

## Architecture

```
  SOURCE ADAPTERS               STREAMING            DATA LAKE (Parquet)
 ┌───────────────┐
 │ Kalshi        │\            ┌──────────┐          ┌────────────────────────┐
 │ Manifold      │ \           │ Redpanda │          │ raw/<source>/date=...  │
 │ Polymarket    │  ──fetch──> │  topics  │ ─consume─>│ clean/<source>/date=.. │
 │ The Odds API  │ /           │(quotes_*)│ normalize └───────────┬────────────┘
 │ NWS weather   │/            └──────────┘ +fee-adj              │
 └───────────────┘             (streaming)  +dedupe               v
        │ (batch: direct land)                            ┌───────────────┐
        └──────────────────────────────────────────────> │ DuckDB        │
                                                          │ warehouse     │
                                                          └───────┬───────┘
                                       ┌──────────────────────────┤ dbt
                                       v                          v
                              dim_event (entity            mart_divergence
                              resolution)            (fee + uncertainty aware,
                                       │              confidence-tiered)
                                       v                          │
                              fact_quotes_with_event              v
                                                            signal_log + scoring
                                                            (hit rate, calibration,
                                                             net PnL, Brier)
                                       ┌──────────────────────────┤
                                       v                          v
                              Streamlit product            Discord alerter
                              (7 pages, read-only)         (above-threshold, read-only)

  analytics.py — confidence, uncertainty, edge decomposition, source reliability
  quality.py   — freshness / nulls / duplicates / bounds → data_quality.parquet
  Orchestration: Dagster.   CI: GitHub Actions (ruff + pytest + dbt build).
```

Data flows in one direction and each stage is independently testable. The
warehouse is the single source of truth the dashboard and alerter read; neither
can write back, and there is no code path to a venue's trading API.

## The analytics: from a price gap to an honest edge

A raw price difference is not an opportunity. EdgeRadar peels every apparent edge
back in layers (`src/edgeradar/analytics.py`, mirrored in `mart_divergence`):

| Layer | Definition | What it answers |
|---|---|---|
| **Raw edge** | `\|price − consensus\|` | How far is this venue from the others? |
| **Fee-adjusted edge** | `raw − trade_cost` | Does it survive the cost to act? |
| **Uncertainty-adjusted edge** | `fee_adj − dispersion` | Does it survive the platforms disagreeing? |

`trade_cost` is a real model: Kalshi's per-contract fee `0.07·P·(1−P)` plus half
the bid/ask spread, in probability units (Manifold/Polymarket/sportsbook feeds
are data-only, cost 0). `dispersion` is the cross-platform price stddev, so an
edge is charged for a noisy consensus.

Each divergence also gets a **confidence tier** (high/medium/low) from how many
independent venues priced the event and how tightly they agree, and each source
gets a **reliability score** (0–100) blending freshness, completeness, and — once
outcomes settle — calibration. Every tier and score comes with a plain-English
reason, never a bare number.

## Data quality & observability

`src/edgeradar/quality.py` scans the lake and writes `data/quality/data_quality.parquet`,
one row per source, covering the boring checks that catch real breakage:
freshness (minutes since last snapshot), quote/market volume, null rate on the
field that matters, **duplicate rate** (a direct test of ingestion idempotency),
probability-bounds violations, and a partial-ingest heuristic (latest snapshot vs
the source's median volume). The dashboard's **Source health** page renders this
as letter grades; `make quality` runs the full lint + test + dbt gate.

## Evaluation & calibration — the honest core

Most "find the edge" projects stop at a leaderboard. EdgeRadar's credibility comes
from refusing to:

- **Signal logging** captures every flagged signal with the prices and
  probabilities *at signal time*, append-only and idempotent.
- **Scoring** records the implied side, whether it won, the predicted probability
  (for calibration), and hypothetical PnL **net of fees**, counted only on the
  tradeable (Kalshi) side.
- **Calibration buckets + Brier score** show predicted vs realized rates.
- A **market-wide calibration backfill** scores already-settled Kalshi markets for
  instant context (rather than waiting days for live signals to resolve).

The dashboard is explicit that this market-wide study is *context*, not a backtest
of EdgeRadar's own live signals — those accumulate forward as logged signals
resolve. See [`FINDINGS.md`](FINDINGS.md) for the real results (including a
favorite-longshot bias across 1,523 settled markets, Brier ≈ 0.067).

## The dashboard (7 pages)

`make dashboard` (http://localhost:8501), read-only throughout:

- **Overview** — product framing and top-line KPIs (sources, quotes,
  cross-platform events, flagged divergences, evaluated outcomes, last ingest).
- **Divergences** — filter/search/sort explorer with confidence chips and a
  per-signal edge breakdown ("raw 0.10 → after cost 0.07 ✅ → after uncertainty
  0.03 ✅").
- **Resolution** — inspect how each cross-platform event was matched, with
  near-miss pairs surfaced for review; when nothing matched, a diagnostics panel
  explains why in plain language instead of a bare empty state.
- **Weather** — NWS forecast vs Kalshi temperature markets, with the model
  assumptions stated up front.
- **Calibration** — Brier score, calibration curve, by-market-type breakdown, and
  an explicit limitations note.
- **Source health** — the data-quality report as reliability grades.
- **System status** — which pipeline stages have produced data.

### Screenshots

Screenshots aren't committed (they'd go stale and the demo data is deterministic
anyway). To capture your own for a portfolio writeup: run `make demo` then
`make dashboard`, and screenshot the Overview, Divergences, and Calibration pages.

## Developer experience

```bash
make doctor        # diagnose env: Python, deps, files, sample data, read-only guardrail
make demo          # fastest offline end-to-end build
make quality       # the full gate: ruff lint + format check, pytest, dbt build/test
make data-quality  # (re)write the source-health report
```

`edgeradar doctor` and `edgeradar quality` are also available as direct CLI
commands. CI (`.github/workflows/ci.yml`) runs the same lint + tests + offline
`dbt build` on every push.

## Tech stack

Python 3.11 with [`uv`](https://docs.astral.sh/uv/) · Parquet data lake · DuckDB
warehouse · dbt (dbt-duckdb) · Redpanda (Kafka-compatible streaming) · Dagster
(orchestration) · Pydantic (typed contracts) · Streamlit (dashboard) · Docker
Compose · ruff + pytest + GitHub Actions.

## Repository layout

```
src/edgeradar/
├── config.py            # typed settings (single source of config)
├── models.py            # MarketQuote / RawRecord contracts + natural key
├── normalize.py         # price → implied probability + vig removal
├── fees.py              # Kalshi fee + spread cost model
├── adapters/            # one SourceAdapter per venue (the key abstraction)
├── targeting.py         # category -> series_ticker / keyword targeting for ingestion
├── streaming/           # producer / consumer / serde
├── entity_resolution.py # cross-platform event matching (blocking + fuzzy + overrides)
├── resolution_diagnostics.py  # why markets did/didn't match (counts, near-miss scores)
├── analytics.py         # confidence, uncertainty, edge decomposition, reliability
├── quality.py           # data-quality / observability report
├── weather.py           # NWS forecast → probability + sigma calibration
├── evaluation.py        # signal logging, scoring, calibration, backfill
├── alerter.py           # Discord notifier (read-only guardrail)
├── orchestration/       # Dagster assets + job
├── dashboard/app.py     # 7-page Streamlit product
└── cli.py               # `edgeradar` CLI (ingest, resolve, evaluate, quality, doctor, …)
dbt/                     # staging macro + marts + tests
tests/                   # unit tests per component
sample_responses/        # committed fixtures for the offline demo
```

## Limitations (read these)

- **Markets are efficient.** Most raw divergences vanish after fees; the system is
  built to prove that to you, not to deny it.
- **Cross-platform comparability is imperfect.** Two venues' "same" market can have
  subtly different resolution criteria; entity resolution is fuzzy and supervised
  by an override table, not perfect. Concretely: macro markets differentiated only
  by *which month's meeting* (not direction) can still merge, since month
  abbreviations vary by platform ("Jun" vs "June") and aren't normalized — see
  [`FINDINGS.md`](FINDINGS.md) for this and other matching edge cases found on
  live data, and how big each fix's effect was.
- **Real cross-platform overlap is genuinely thin in places.** Polymarket's weather
  markets are international cities (Tokyo, Beijing, Cape Town, ...); Kalshi's are
  US cities. That's real non-overlap, not a bug — the dashboard's Resolution page
  says so explicitly rather than forcing a match.
- **Live signal evaluation is forward-accumulating.** A handful of resolved signals
  proves nothing; trust calibration only at scale.
- **The weather model is deliberately simple** (a calibrated Normal around the NWS
  high) — a worked example of forecast-vs-market comparison, not a meteorology model.

## Roadmap

More venues behind the same adapter interface · a Postgres warehouse target for
dbt · longer-horizon live signal calibration · richer per-source reliability
weighting in the consensus · normalizing month-name abbreviations in the
predicate guard (see Limitations above).

## What this demonstrates

Data engineering and streaming, fuzzy entity resolution, forecast evaluation and
calibration, data-quality/observability practice, dashboard and product design,
human-in-the-loop and responsible-by-design decision support, and software
reliability (typed contracts, tests, CI, one-command reproducible demo).

## License

MIT.
