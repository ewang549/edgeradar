"""Price → implied-probability conversions (and vig removal).

Phase 0 ships the formulas as documented, tested helpers so the math lives in one
place. Adapters call into these in Phase 1. Each function explains its source
convention. Nothing here touches the network.

Key idea — *implied probability*: a market price is a bet on an outcome, so it can
be read as the market's estimate of that outcome's probability. Different
platforms quote differently (American odds, decimal odds, cents, share prices),
so we convert all of them to a single number in (0,1) to compare like-for-like.

Key idea — *vig / overround*: bookmakers price both sides so the implied
probabilities sum to MORE than 1; the excess is their margin ("vig"). To compare
a sportsbook to a fair market we must remove it. The simplest method (here) is
proportional normalization: divide each side by the sum of both sides.
"""

from __future__ import annotations


def american_to_prob(odds: int) -> float:
    """Convert American moneyline odds to a raw implied probability in (0,1).

    +150 -> 100/(150+100) = 0.40 ; -200 -> 200/(200+100) = 0.667.
    """
    if odds == 0:
        raise ValueError("American odds cannot be 0.")
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return -odds / (-odds + 100.0)


def decimal_to_prob(decimal_odds: float) -> float:
    """Convert decimal odds (e.g. 2.50) to a raw implied probability in (0,1)."""
    if decimal_odds <= 1.0:
        raise ValueError("Decimal odds must be > 1.0.")
    return 1.0 / decimal_odds


def kalshi_cents_to_prob(price_cents: float) -> float:
    """Convert a Kalshi YES price in cents (1..99) to probability (0.01..0.99).

    Kalshi contracts settle at $1.00 (100¢) if YES, $0 if NO, so the YES price in
    dollars *is* the market-implied probability.
    """
    if not (0 < price_cents < 100):
        raise ValueError("Kalshi price must be strictly between 0 and 100 cents.")
    return price_cents / 100.0


def share_price_to_prob(share_price: float) -> float:
    """Convert a Polymarket-style share price in (0,1) to probability.

    Shares pay $1 if the outcome occurs, so the share price already equals the
    implied probability. Included for completeness (Polymarket lands in a later
    phase as a data-only consensus signal).
    """
    if not (0.0 < share_price < 1.0):
        raise ValueError("Share price must be strictly between 0 and 1.")
    return share_price


def remove_vig_two_way(prob_a: float, prob_b: float) -> tuple[float, float]:
    """Proportionally normalize two raw probabilities to remove the overround.

    Returns (fair_a, fair_b) summing to 1.0. Example: (0.55, 0.55) -> (0.5, 0.5).
    """
    total = prob_a + prob_b
    if total <= 0:
        raise ValueError("Probabilities must be positive.")
    return prob_a / total, prob_b / total
