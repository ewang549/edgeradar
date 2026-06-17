"""Shared data contracts.

`MarketQuote` is the normalized record every source adapter must produce. Keeping
this contract in one place is what lets us add a new platform by writing a single
adapter class — the rest of the pipeline only ever sees `MarketQuote`s.

Natural key (idempotency): (source, market_id, outcome, snapshot_ts).
Reruns that re-fetch the same snapshot must never create duplicates downstream.
All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator


class RawRecord(BaseModel):
    """A single raw payload as fetched from a source, before normalization.

    We persist this verbatim (the raw zone) so we can re-derive normalized
    values later if our math changes, and so signals stay auditable.
    """

    source: str
    market_id: str
    snapshot_ts: datetime  # when WE fetched it (UTC)
    payload: dict


class MarketQuote(BaseModel):
    """Normalized, cross-platform quote for one outcome of one market.

    `implied_prob` is the raw market-implied probability; `fee_adj_prob` is that
    probability after modeling the cost to act (fees + half-spread). A divergence
    only matters once measured against `fee_adj_prob`. In Phase 0 these fields
    exist but the normalization math is stubbed (see normalize.py).
    """

    source: str = Field(description="Platform slug, e.g. 'kalshi', 'manifold'.")
    market_id: str = Field(description="Platform-native market identifier.")
    outcome: str = Field(description="Outcome label, e.g. 'YES'/'NO' or a team.")
    title: str = Field(description="Human-readable market/question title.")

    price: Decimal = Field(
        description="Native price as quoted (cents, decimal odds, share price...)."
    )
    implied_prob: float | None = Field(
        default=None,
        description="Market-implied probability in (0,1), vig-removed where applicable.",
    )
    fee_adj_prob: float | None = Field(
        default=None,
        description=(
            "Fair point estimate used for comparison (currently the mid/implied prob). "
            "The directional cost to trade is carried separately in trade_cost, because "
            "the adjustment's sign depends on which side you'd take — decided in the signal engine."
        ),
    )
    spread: float | None = Field(
        default=None,
        description="Bid/ask spread in probability units, if the source quotes a book.",
    )
    trade_cost: float | None = Field(
        default=None, description="Estimated cost to act (half-spread + fee) in probability units."
    )

    snapshot_ts: datetime = Field(
        description="UTC time WE fetched the quote (natural-key component)."
    )
    valid_from: datetime | None = Field(
        default=None, description="Start of the quote's validity window (UTC), if known."
    )
    close_ts: datetime | None = Field(
        default=None, description="When the market closes/resolves (UTC), if known."
    )

    @field_validator("implied_prob", "fee_adj_prob")
    @classmethod
    def _prob_in_unit_interval(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if not (0.0 < v < 1.0):
            raise ValueError(f"probability must be in the open interval (0,1), got {v}")
        return v

    @property
    def natural_key(self) -> tuple[str, str, str, datetime]:
        """The idempotency key used everywhere downstream."""
        return (self.source, self.market_id, self.outcome, self.snapshot_ts)
