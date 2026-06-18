# EdgeRadar — Honest Findings

> Scope note: except where a specific live sample size is stated (Finding 2 was
> measured over 1,523 settled markets), the numbers below are illustrative — drawn
> from the committed sample dataset and the project's cost model, **not** a large
> live backtest. The point of EdgeRadar's evaluation layer is precisely that you
> *cannot* claim an edge until it survives scoring across many real, resolved
> events. Each finding is explicit about what would be needed to confirm it. This
> file is the project's conscience: it exists so the dashboard's nice numbers never
> get mistaken for proof.

## Finding: at retail size, trading cost — not mispricing — dominates cross-platform divergence

The headline lesson from building and running EdgeRadar is that **the cost to act is
large relative to the price gaps you actually observe**, so most "divergences" are
not edges once you account for fees and spread.

Concretely, the Kalshi fee model is `fee ≈ 0.07 · P · (1 − P)` per contract, which
peaks at **0.0175** (1.75 probability points) at `P = 0.5`. Add half a typical
2–3¢ bid/ask spread (~0.010–0.015) and the round-number cost to take a position near
even odds is roughly **0.025–0.03 in probability units**. So a cross-platform gap has
to clear ~2.5–3 points *just to break even*.

In the sample data this is visible directly:

| Event | Kalshi price | Consensus | Raw gap | Cost | **Net edge** |
|-------|-------------:|----------:|--------:|-----:|-------------:|
| Celtics vs Lakers | 0.910 | 0.880 | 0.030 | 0.016 | **0.014** |
| NYC high > 82.5°F (divergence) | 0.465 | 0.500 | 0.035 | 0.032 | **0.003** |

A 3-point gap collapses to a ~1-point net edge after fees — and a 1-point net edge is
well inside the noise of how well two different venues' prices should even be expected
to agree. The implication for the project: **ranking signals by the raw deviation
would be actively misleading; ranking by `edge_net` (deviation − cost) is the only
honest view, and it pushes most cross-platform divergences below the threshold.**

A second, related caveat the build made obvious: **Manifold is play money.** Where
Manifold disagrees with Kalshi, that is usually not tradable signal — Manifold prices
aren't "sharp," so treating them as consensus is a reference point at best. EdgeRadar
counts PnL only on the tradeable (Kalshi) side for exactly this reason.

## The more promising lead: the weather module

The weather edge is structurally more interesting because it compares a market to an
**independent physical forecast** (NWS), not to another market. On the sample day the
gap was large — forecast-implied `P(high > 82.5°F) ≈ 0.81` vs Kalshi `0.47`, a net
edge of ~0.31 even after cost. If that held up, it would be a real edge.

But it rests on assumptions that are *unproven here*:

1. The forecast→probability step is a single Normal with a hand-picked `sigma = 4°F`.
   That sigma is a guess, not a fitted value.
2. It's one day. Daily-temperature markets are exactly the place where the market may
   know something the point forecast doesn't (e.g. intraday timing, station vs grid).

## What would actually confirm (or kill) these findings

- Run the live pipeline daily for several weeks so the `signal_log` accumulates
  **dozens to hundreds** of resolved signals.
- Read the calibration report (`mart_calibration`): does realized hit rate track the
  predicted probability bucket-by-bucket? For the weather model specifically, are the
  forecast-implied probabilities well-calibrated, or are the tails overconfident?
- Fit `sigma` from historical NWS-forecast-vs-observed error instead of guessing it.
- Only if net-of-fee PnL is positive and calibration holds across many events should
  any signal be treated as a real edge — and even then, a human reviews each one.

The honest expected outcome, consistent with prediction markets being largely
efficient: most divergence signals are fee-dominated noise, and the realistic prize is
a strong data-engineering portfolio project plus, at most, small edges in the corners
(like well-modeled weather) — not riches.

## Finding 2: Kalshi closing prices are well-calibrated, with a favorite-longshot bias

`edgeradar backfill` scores already-settled Kalshi markets immediately — each settled
binary market exposes its closing price (≈ implied probability) and its actual
result, so calibration can be measured *now* instead of waiting for new signals to
resolve. Over **1,523** settled markets in one pull:

- **Brier score 0.067** — low, i.e. closing prices are well-calibrated overall.
- **Favorite accuracy 91%** — but inflated by how lopsided the population is (most
  markets resolve near 0 or 1), so this number alone is not meaningful.

The calibration curve (closing price bucket → realized frequency) shows the real
structure:

| price bucket | predicted | realized |
|---|---:|---:|
| 0.1 | 0.095 | 0.010 |
| 0.2 | 0.194 | 0.089 |
| 0.3 | 0.294 | 0.168 |
| 0.4 | 0.400 | 0.330 |
| 0.5 | 0.495 | 0.547 |
| 0.7 | 0.699 | 0.705 |
| 0.9 | 0.898 | 0.846 |

In the **low buckets (0.1–0.4) realized is consistently below predicted**: cheap
"yes" contracts happened *less often* than their price implied — they're overpriced.
The mid/high range is well-calibrated. This is the classic **favorite-longshot
bias** (longshots are systematically too expensive), reproduced cleanly from live data.

**Caveats (why this isn't a money printer):**
- The settled feed is dominated by **high-frequency markets** (15-minute crypto,
  sports combos); the bias may not transfer to the slower markets you'd actually
  trade. The `backfill` per-market-type breakdown exists to check this.
- It's measured at **closing price**, which can be thin/stale on low-volume markets.
- Fading overpriced longshots means selling NO on cheap contracts; whether that
  survives Kalshi fees, liquidity, and the bias *persisting* is a separate question
  the PnL scorer (`make evaluate`) must answer. This is a calibration finding, not a
  trade recommendation.

## Data-quality gotchas found on live data

Running EdgeRadar against real APIs (rather than the tidy sample fixtures) surfaced
four bugs that the demo data never would have. Each followed the same tell: a
suspiciously large "edge" was a defect in our own pipeline, not free money. All four
were fixed and now have regression tests; every external call was also made
fail-soft so one bad input can't crash the daily run.

1. **Stale NWS grid id (404).** The weather module hardcoded an NWS gridpoint
   (`OKX/33,35`) that returned 404 on live data — those grid ids aren't stable.
   *Fix:* resolve the forecast URL at runtime from lat/long via `/points/{lat},{lon}`,
   and skip (don't crash) on any NWS error.

2. **Sports markets parsed as temperature.** The weather detector matched any title
   containing "new york" + "over `<number>`", so "New York Mets win by over 1.5 runs"
   was read as a 1.5°F threshold and produced fake ~0.9 edges. *Fix:* the threshold
   number must be followed by a temperature unit (°/F/degrees) **and** the title must
   contain a temperature keyword.

3. **Out-of-bounds datetime (year 5555).** Some markets use a far-future "never
   closes" sentinel date that overflows pandas' datetime64[ns] range (~1678–2262),
   crashing the Parquet reader during concat. *Fix:* clamp out-of-range timestamps to
   "unknown" at ingestion, and coerce/clamp on read so existing bad files are tolerated.

4. **Entity-resolution over-merging.** A ladder of near-identical titles ("Houston
   96°F or higher", "97°F or higher", …) collapsed into one event because the titles
   are ~95% similar, producing spurious cross-platform divergences across *different
   thresholds*. *Fix:* require the set of numbers in two titles to match before a
   fuzzy pair can group (manual overrides still win).

The meta-lesson: demo data tests the happy path; live data tests your assumptions.
The value of the evaluation layer and these guards is that the system fails *loudly
in tests* and *softly in production*, rather than silently emitting garbage signals.
