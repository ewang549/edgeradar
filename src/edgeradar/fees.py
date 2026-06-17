"""Fee / spread cost model.

A divergence only matters if it survives the cost to ACT on it. This module
estimates that cost in probability units (a Kalshi contract settles at $1, so a
price/cost in dollars is directly comparable to a probability).

Two components:

* **Half-spread** — to take a position you cross the bid/ask, paying about half
  the spread away from the midpoint.
* **Trading fee** — Kalshi charges a per-contract fee. Their published general
  schedule is ``fee = round_up(0.07 * C * P * (1 - P))`` for ``C`` contracts at
  price ``P``. Per contract (``C = 1``) that's ``0.07 * P * (1 - P)`` dollars —
  largest near P = 0.5, ~0 near the extremes. We keep it continuous here; the
  real exchange rounds up to the cent (encode that in Phase 6 if it matters).

Manifold is play money, so its trading cost is treated as 0 — it serves only as a
consensus reference, never as something you'd actually trade.

The signal engine compares an event's cross-platform deviation to this cost: a
quote is only an actionable edge when ``|deviation| > trade_cost``.
"""

from __future__ import annotations

KALSHI_FEE_COEFFICIENT = 0.07  # Kalshi general fee schedule coefficient


def kalshi_fee(price: float) -> float:
    """Per-contract Kalshi trading fee (dollars) at the given price in (0,1)."""
    p = min(max(price, 0.0), 1.0)
    return KALSHI_FEE_COEFFICIENT * p * (1.0 - p)


def trading_cost(source: str, price: float | None, spread: float | None) -> float | None:
    """Estimate the cost (in probability units) to act on a quote.

    Returns None when there's no usable price. Manifold (play money) -> 0.0.
    """
    if price is None:
        return None
    if source == "kalshi":
        half_spread = (spread or 0.0) / 2.0
        return round(kalshi_fee(price) + half_spread, 6)
    # Play-money / data-only sources have no real trading cost.
    return 0.0
