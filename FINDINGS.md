# EdgeRadar — One Honest Finding

> Scope note: the numbers below are illustrative, drawn from the committed sample
> dataset and the project's cost model — **not** from a large live backtest. The
> point of EdgeRadar's evaluation layer (Phase 6) is precisely that you *cannot*
> claim an edge until it survives scoring across many real, resolved events. This
> write-up states a finding and is explicit about what would be needed to confirm it.

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
