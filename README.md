# EdgeRadar

![CI](https://github.com/ethanwang/edgeradar/actions/workflows/ci.yml/badge.svg)

A **read-only** cross-platform prediction-market mispricing engine. It ingests
live odds/prices from several prediction markets and sportsbooks, normalizes
everything to implied probabilities, resolves which markets refer to the same
real-world event, and surfaces divergences (one platform pricing an event
materially differently from the consensus) **net of fees and spread**. A weather
module compares official NWS temperature forecasts to Kalshi daily-temperature
markets.

> ⚠️ **EdgeRadar never places orders or executes trades.** It ingests public data
> and *notifies a human*, who makes every decision manually. Every signal is
> logged and later scored against the real outcome, so we can tell whether an
> "edge" is real *before* acting on it. See [`ARCHITECTURE.md`](ARCHITECTURE.md).
> Prediction markets are mostly efficient — the realistic payoff is a strong
> portfolio project, not riches.

## Architecture (target end state)

```
  SOURCE ADAPTERS                STREAMING            DATA LAKE (MinIO, Parquet)
 ┌───────────────┐
 │ Kalshi        │\            ┌──────────┐          ┌────────────────────────┐
 │ Manifold      │ \           │ Redpanda │          │ raw/<source>/date=...  │
 │ Metaculus     │  ──fetch──> │  topics  │ ─consume─>│ clean/<source>/date=.. │
 │ The Odds API  │ /           │(quotes_*)│  normalize└───────────┬────────────┘
 │ NWS weather   │/            └──────────┘  +fee-adj             │
 └───────────────┘             (Phase 3)     +dedupe              v
        │ (Phase 1: direct land)                          ┌───────────────┐
        └────────────────────────────────────────────────>│ DuckDB        │
                                                           │ warehouse     │
                                                           └───────┬───────┘
                                                                   │ dbt (Phase 2/4/5)
                                          ┌────────────────────────┼───────────────────┐
                                          v                        v                   v
                                   stg_<source>            dim_event (entity      mart_divergence
                                          │                resolution, Phase 4)   mart_weather_edge
                                          v                        │              (Phase 5, fee-aware)
                                   fact_market_quotes <────────────┘                   │
                                   (event, platform, outcome,                          v
                                    implied_prob, fee_adj_prob, ts)              signal_log + scoring
                                                                                 (Phase 6: hit rate,
                                                                                  calibration, net PnL)
                                                                                        │
                                                          ┌─────────────────────────────┴───┐
                                                          v                                  v
                                                  Streamlit dashboard              Discord alerter
                                                  (Phase 7, read-only)             (Phase 7, above-
                                                                                    threshold signals)

  Orchestration: Dagster (batch resolution + scoring).  CI: GitHub Actions (lint + dbt build + tests).
  Everything runs locally & free via Docker Compose.
```

## What works so far

**Phase 0 — Scaffold.**
- Repo structure + Python package skeleton (`src/edgeradar`).
- The pluggable `SourceAdapter` interface (`adapters/base.py`) — adding a platform = one class.
- Typed settings module (`config.py`); nothing hardcoded, all config from env/`.env`.
- Documented, tested price→implied-probability math (`normalize.py`).
- `docker-compose.yml` with **MinIO** (S3-compatible lake) + bucket init + a Python `app` container; **DuckDB** is file-based (no container).
- `Makefile`, `.env.example`, `.gitignore`, smoke tests, this README, and `ARCHITECTURE.md`.

**Phase 1 — First two adapters (current).**
- **Manifold** adapter (`adapters/manifold.py`): no-auth fetch of binary markets; `probability` → `implied_prob` directly.
- **Kalshi** adapter (`adapters/kalshi.py`): public read-only feed; YES bid/ask **midpoint** → `implied_prob` (fee/spread modeled later, in Phase 5).
- **Local data lake** (`storage.py`): lands **raw** JSON payloads and **clean** normalized quotes as Parquet, partitioned `source=/date=`, deduped on the natural key. (MinIO/S3 writer comes in a later phase behind the same interface.)
- **Ingestion runner + registry** (`ingest.py`) wired to the `edgeradar ingest` CLI and `make ingest`.
- **`--dry-run`** mode for both adapters reads committed `sample_responses/` fixtures — fully offline, spends no API quota.
- Idempotent: re-running a dry-run overwrites the same snapshot file (no duplicate accumulation).

**Phase 2 — Warehouse + dbt (current).**
- **dbt-duckdb** project under `dbt/` (project + in-repo `profiles.yml`).
- **Staging views** `stg_manifold`, `stg_kalshi`: read the clean Parquet lake directly, standardize types, convert NaN probabilities to NULL, add a surrogate `quote_key`.
- **`fact_market_quotes`** table: all sources unified at grain `(source, market_id, outcome, snapshot_ts)` — the single table everything downstream reads.
- **dbt tests** (21): `quote_key` unique + not-null (proves idempotent ingestion), `source`/`outcome`/`price`/`snapshot_ts` not-null, `accepted_values` on `source`, and a singular test enforcing every non-null probability is strictly inside (0,1).
- `make dbt` builds + tests; `make dbt-test` runs tests only.

**Phase 3 — Streaming (current).**
- **Redpanda** broker (Kafka-compatible, single container) + **Redpanda Console** UI at http://localhost:8080.
- **Producer** (`streaming/producer.py`): adapters fetch raw quotes and publish them to the `quotes_raw` topic (keyed `source:market_id`). `make produce`.
- **Consumer** (`streaming/consumer.py`): drains the topic, normalizes (reusing the same adapter logic), applies the **fee-adjustment hook** (`fees.py` — honest placeholder until Phase 5), dedupes, and writes the clean zone. `make consume`.
- Pure, unit-tested **serde** and **stream-normalize** layers; the broker is the only non-deterministic part.
- Idempotent: produce → consume → produce → consume leaves the clean zone duplicate-free.

**Phase 4 — Entity resolution (current).**
- **`entity_resolution.py`**: a layered matcher — category **blocking** → fuzzy **title + date** scoring (token-set Jaccard + sequence ratio, std-lib only) → a **manual override** table — producing an `event_map` (market → canonical `event_id`) with a **confidence** on every match.
- **Manual overrides** (`seeds/event_overrides.csv`): force a match or block a pair; always beats the fuzzy score.
- **Reviewable**: `make resolve` prints proposed matches (and near-threshold pairs) with scores so mismatches are easy to catch and correct.
- **dbt**: `stg_event_map` → `dim_event` (one row per event, sources, confidence) and `fact_quotes_with_event` (every quote tagged with its `event_id` — what Phase 5 reads).
- On the sample data the two known cross-platform pairs (an NBA game and an NYC-temperature market) group correctly; unrelated markets stay singletons.

**Phase 5 — Signal engine + weather module (current).**
- **Fee/spread cost model** (`fees.py`): Kalshi per-contract fee `0.07·P·(1−P)` + half the bid/ask spread, in probability units. Manifold (play money) → 0. Computed in `normalize` so both batch and stream paths carry `spread` + `trade_cost`.
- **`mart_divergence`**: per market in a multi-platform event, deviation from **leave-one-out consensus**, and `edge_net = |deviation| − trade_cost`, ranked, with an `is_signal` flag. Review aid only.
- **Weather module** (`weather.py`): pulls NWS forecasts, turns a forecast high into `P(high > threshold)` via a Normal model, parses Kalshi temperature-market thresholds, and computes the fee-aware edge → **`mart_weather_edge`**. `make weather`.
- On sample data: the NBA divergence is flagged (and shrinks correctly after Kalshi fees), and the NYC weather market shows a large edge (forecast ~0.81 vs Kalshi 0.47).

**Phase 6 — Evaluation / backtest (current).** The credibility core.
- **`signal_log`** (`evaluation.py`): appends every currently-flagged signal — with the prices and probabilities *at signal time* — to an append-only log (idempotent on a signal key).
- **Resolutions** (`seeds/resolutions.csv`): known outcomes (live: Kalshi settled `result` + NWS observed highs).
- **Scoring**: per signal — the implied side, whether it won (`hit`), the model's predicted probability (for calibration), and hypothetical **PnL net of fees**, summed only over the *tradeable* (Kalshi) side since Manifold is play money. `make evaluate` prints hit rate, calibration, and net PnL.
- **dbt eval marts** (tag `eval`): `stg_signal_log`, `mart_signal_scores`, `mart_calibration` — kept out of the default build so it never depends on eval outputs.
- **Dagster** (`orchestration/definitions.py`): asset graph `signal_log → scored_signals → eval_dbt_models`, viewable at `make dagster` (http://localhost:3000).

**Phase 7 — Serving + alerts (current).**
- **Streamlit dashboard** (`dashboard/app.py`, `make dashboard`, http://localhost:8501): divergence leaderboard, per-event cross-platform prices, weather-edge panel, and the evaluation/calibration report. Read-only views over the warehouse.
- **Discord alerter** (`alerter.py`, `make alert`): posts above-threshold signals (`alert_min_edge`, default 0.05) to a Discord webhook; `--dry-run` prints instead. Asserts the read-only guardrail and refuses to run if order execution is ever enabled.

**Phase 8 — Polish + CI (current).**
- **GitHub Actions** (`.github/workflows/ci.yml`): lint (ruff) + tests (pytest) + a full `dbt build` (core + eval) on the committed sample dataset — regenerated offline from the dry-run fixtures, so CI needs no APIs, Docker, or services.
- **[`FINDINGS.md`](FINDINGS.md)**: one honest finding (trading cost dominates cross-platform divergence at retail size) with its limitations.
- A 60–90s **demo script** (below).

All eight phases are built. EdgeRadar is feature-complete for its design scope.

## Tech stack

Python 3.11+ with [`uv`](https://docs.astral.sh/uv/) (a fast Rust-based replacement
for pip+venv) · MinIO (object storage) · DuckDB → Postgres-ready warehouse · dbt
(dbt-duckdb) · Redpanda (Kafka-compatible streaming) · Dagster (orchestration) ·
Great Expectations + dbt tests (data quality) · Streamlit (dashboard) · Docker
Compose · GitHub Actions (CI).

## Quick start (Phase 0)

```bash
# 1. Configuration
cp .env.example .env            # defaults are fine for local Phase 0

# 2. Bring up the stack (needs Docker Desktop running)
make up                         # builds the app image, starts MinIO + bucket init

# 3. Verify (see "Verify the Phase 0 gate" below)
make ps                         # services healthy?
open http://localhost:9001      # MinIO console (login with MINIO_ROOT_USER/PASSWORD)
```

You can also work outside Docker:

```bash
make install                    # uv creates .venv and installs deps
uv run pytest                   # run the smoke tests
uv run edgeradar config-check   # confirm settings load
```

## Verify the Phase 0 gate

1. `make up` completes and `make ps` shows **minio** as `healthy` and
   **createbuckets** as `Exited (0)` and **app** as `Up`.
2. Open the MinIO console at **http://localhost:9001**, log in with the
   credentials from `.env`, and confirm the **`edgeradar`** bucket exists.
3. `make config-check` prints settings and reports `order execution = False`.
4. `uv run pytest` (or `make test`) passes the smoke tests.

When all four hold, the gate is green.

## Verify the Phase 1 gate

With the stack up (`make up`):

1. **Offline dry-run** (no network, no quota):
   ```bash
   make ingest SOURCE=all ARGS=--dry-run
   ```
   You should see both sources land quotes, e.g. `manifold ... quotes=2` and
   `kalshi ... quotes=3`, each pointing at a `clean/.../snapshot=...parquet` file.

2. **Inspect the Parquet that landed:**
   ```bash
   docker compose exec app python -c "from edgeradar.storage import read_quotes; \
   print(read_quotes()[['source','market_id','outcome','price','implied_prob','title']].to_string(index=False))"
   ```
   Kalshi rows show bid/ask midpoints; the illiquid sample row shows `implied_prob = NaN`
   (we never fabricate a probability).

3. **Idempotency:** run the dry-run command twice; the quote count stays the same
   (re-running overwrites the same snapshot rather than duplicating).

4. **Live fetch** (real public data; Manifold needs no key, Kalshi reads are public):
   ```bash
   make ingest SOURCE=manifold
   make ingest SOURCE=kalshi
   ```

5. **Tests:** `make test` → 9 passing (Phase 0 + Phase 1).

## Verify the Phase 2 gate

dbt (data build tool) runs SQL transformations and data-quality tests. Because we
added a new dependency (`dbt-duckdb`), rebuild the image once:

```bash
make down && make up          # rebuilds the app image with dbt installed
```

Then:

1. **Land some data** (dbt reads the clean Parquet lake):
   ```bash
   make ingest SOURCE=all ARGS=--dry-run     # or live: make ingest SOURCE=manifold
   ```

2. **Build + test the warehouse:**
   ```bash
   make dbt
   ```
   Expect `Completed successfully` and `PASS=24 ... ERROR=0` (2 views + 1 table + 21 tests).

3. **Query across sources:**
   ```bash
   docker compose exec app python -c "import duckdb; \
   print(duckdb.connect('data/warehouse/edgeradar.duckdb').sql( \
   'select source, count(*) n, count(implied_prob) with_prob from fact_market_quotes group by 1').to_df())"
   ```
   You should see rows for both `manifold` and `kalshi`.

The gate is green when `make dbt` passes all tests and `fact_market_quotes`
contains rows from both sources.

## Verify the Phase 3 gate

Phase 3 adds the Redpanda broker and a new dependency (`confluent-kafka`), so
rebuild + restart once:

```bash
make down && make up          # now also starts redpanda + redpanda-console
make ps                       # redpanda should be (healthy)
```

Then drive the streaming path (offline with --dry-run):

1. **Produce** raw quotes to the topic:
   ```bash
   make produce SOURCE=all ARGS=--dry-run     # "produced 8 message(s)"
   ```

2. **Consume**, normalize, and land clean Parquet:
   ```bash
   make consume                               # "consumed 8 message(s) -> 5 quote(s)"
   ```

3. **Inspect** the stream in the browser at http://localhost:8080 (topic `quotes_raw`).

4. **Idempotency:** run produce + consume again; the clean zone stays duplicate-free:
   ```bash
   docker compose exec app python -c "from edgeradar.storage import read_quotes; print(len(read_quotes()), 'unique quotes')"
   ```

5. **Tests:** `make test` → 12 passing.

The gate is green when produce → consume lands clean quotes and reruns don't
create duplicates. (For live data, drop `ARGS=--dry-run`.)

## Verify the Phase 4 gate

No rebuild needed (pure Python + dbt SQL). The run order is **land data → resolve
→ dbt** (dbt's `dim_event` reads what `resolve` writes):

1. **Land data** (if you haven't this session):
   ```bash
   make ingest SOURCE=all ARGS=--dry-run
   ```

2. **Resolve events** — group same-event markets across platforms:
   ```bash
   make resolve
   ```
   You should see `2 cross-platform` events, and a review list showing the NBA
   pair (~0.98) and the NYC-temperature pair (~0.89) as `match`.

3. **Build the warehouse** (now includes `dim_event`):
   ```bash
   make dbt
   ```
   Expect `PASS=39 ... ERROR=0`.

4. **See the cross-platform prices line up:**
   ```bash
   docker compose exec app python -c "import duckdb; print(duckdb.connect('data/warehouse/edgeradar.duckdb').sql(\"select event_id, source, round(implied_prob,3) p, left(title,30) title from fact_quotes_with_event where event_id in (select event_id from dim_event where n_sources>1) order by event_id, source\").to_df())"
   ```
   Each cross-platform `event_id` shows one row per platform — e.g. the NBA event
   at Kalshi 0.91 vs Manifold 0.88. That divergence is what Phase 5 will score.

To **correct** a wrong match, add a row to `seeds/event_overrides.csv`
(`match` or `block`) and re-run `make resolve`.

The gate is green when the known cross-platform markets are grouped under shared
`event_id`s, `make dbt` passes, and mismatches are reviewable via `make resolve`.

## Verify the Phase 5 gate

No rebuild needed. Run order: **land → resolve → weather → dbt**.

```bash
make ingest SOURCE=all ARGS=--dry-run
make resolve
make weather ARGS=--dry-run     # prints the NYC temp market as a SIGNAL (~+0.31)
make dbt                        # expect PASS=47, ERROR=0
```

See the ranked signals:

```bash
docker compose exec app python -c "import duckdb; print(duckdb.connect('data/warehouse/edgeradar.duckdb').sql('select source, round(implied_prob,3) p, round(consensus,3) consensus, round(trade_cost,4) cost_, round(edge_net,3) edge_net, is_signal from mart_divergence').to_df())"
docker compose exec app python -c "import duckdb; print(duckdb.connect('data/warehouse/edgeradar.duckdb').sql('select location, round(forecast_prob,3) fc, round(kalshi_prob,3) kalshi, round(edge_net,3) edge_net, is_signal from mart_weather_edge').to_df())"
```

The gate is green when `mart_divergence` and `mart_weather_edge` both produce
ranked rows whose `edge_net` is net of `trade_cost`. (Live: drop `--dry-run` and
ensure `NWS_USER_AGENT` in `.env` has your contact — NWS requires it.)

## Verify the Phase 6 gate

Phase 6 adds Dagster, so rebuild the image once:

```bash
make down && make up
```

Then run the full pipeline and score the signals:

```bash
make ingest SOURCE=all ARGS=--dry-run
make resolve
make weather ARGS=--dry-run
make dbt                 # core marts (PASS=47); excludes the eval models
make evaluate            # logs signals + scores them
```

`make evaluate` prints, for the logged signals: how many resolved, the **hit rate**,
the **net PnL** over the tradeable (Kalshi) side, and a **calibration** table
(predicted probability vs realized rate). On the sample data: 5 signals, all
resolved, hit rate 0.6, net PnL ≈ +0.90 per contract.

Optional — the orchestrated view and the dbt eval marts:

```bash
make dagster             # open http://localhost:3000, materialize the asset graph
# or build the eval dbt tables directly:
docker compose exec app sh -c "dbt build --select tag:eval --project-dir dbt --profiles-dir dbt"
```

The gate is green when, for past signals, you can see whether they would have been
right (`hit`) and the edge net of fees (`pnl_net`, `mart_signal_scores`,
`mart_calibration`).

> Reality check: a handful of sample signals proves nothing — the calibration is
> only meaningful across many real, resolved events. This phase exists precisely so
> you don't fool yourself: trust the edge only once the numbers hold up at scale.

## Verify the Phase 7 gate

Phase 7 adds Streamlit, so rebuild once:

```bash
make down && make up
```

Make sure the warehouse is populated (Phases 1–6), then:

1. **Dashboard:**
   ```bash
   make dashboard          # open http://localhost:8501
   ```
   Four tabs: divergence leaderboard, per-event prices, weather edge, and the
   evaluation/calibration report. (Ctrl-C in the terminal to stop it.)

2. **Alerter** — fire a test alert (prints instead of sending with `--dry-run`):
   ```bash
   make alert ARGS=--dry-run
   ```
   On the sample data this shows the NYC weather signal (~+0.31). To actually send
   to Discord, put a webhook URL in `.env` as `DISCORD_WEBHOOK_URL` and run
   `make alert` (no `--dry-run`).

The gate is green when the dashboard renders the marts and a test alert fires.
Everything here is read-only — the alerter refuses to run if `ENABLE_ORDER_EXECUTION`
is ever set true.

## Demo script (60–90 seconds)

A walkthrough for showing the project end to end (offline, deterministic):

1. **"It's a local, free, containerized data platform."** `make up` → show
   `make ps` (MinIO + Redpanda healthy), open the MinIO console (9001) and Redpanda
   Console (8080).
2. **"Adapters ingest public markets through a Kafka stream."**
   `make produce SOURCE=all ARGS=--dry-run` then `make consume` → show the
   `quotes_raw` messages in Redpanda Console, then the clean Parquet that landed.
3. **"dbt turns the lake into a tested warehouse."** `make dbt` → point out
   `PASS=47`, and that `fact_market_quotes` unifies both platforms.
4. **"Entity resolution finds the same event across platforms."** `make resolve`
   → show the NBA + weather pairs grouped with confidence scores.
5. **"The signal engine ranks mispricings net of fees, plus a weather edge."**
   `make weather ARGS=--dry-run`; open the **dashboard** (`make dashboard`, 8501)
   → divergence leaderboard, per-event prices, weather panel.
6. **"And the honest core: we score signals against outcomes."** `make evaluate`
   → hit rate, calibration, net PnL; open the dashboard's Evaluation tab.
7. **"An alert notifies me — it never trades."** `make alert ARGS=--dry-run`.
   Close on the read-only guardrail and [`FINDINGS.md`](FINDINGS.md): cost dominates,
   so calibration — not a pretty number — is what decides if an edge is real.

## Continuous integration

Every push runs `.github/workflows/ci.yml`: `ruff` lint + format check, `pytest`,
and a full `dbt build` (core + eval) on the sample dataset regenerated from the
committed `--dry-run` fixtures. To run the same checks locally:

```bash
make lint && make test && \
  make ingest SOURCE=all ARGS=--dry-run && make resolve && make weather ARGS=--dry-run && \
  make dbt && make evaluate
```

## Repository layout

```
.
├── docker-compose.yml      # MinIO + bucket init + app container
├── Dockerfile              # uv-based Python app image
├── Makefile                # make up / ingest / dbt / test / dashboard
├── pyproject.toml          # deps + tooling (ruff, mypy, pytest)
├── .env.example            # config template (copy to .env; .env is git-ignored)
├── README.md
├── ARCHITECTURE.md         # design decisions (read-only, entity resolution, fees)
├── src/edgeradar/
│   ├── config.py           # typed settings (single source of config)
│   ├── models.py           # MarketQuote / RawRecord contracts + natural key
│   ├── normalize.py        # price -> implied probability + vig removal
│   ├── cli.py              # `edgeradar` CLI (version, config-check, ingest stub)
│   └── adapters/
│       └── base.py         # SourceAdapter interface (the key abstraction)
├── tests/                  # one test per component; grows each phase
├── sample_responses/       # saved API responses for --dry-run (committed)
├── dbt/                    # dbt project (Phase 2)
└── data/                   # local lake + DuckDB file (git-ignored)
```

## License

MIT.
