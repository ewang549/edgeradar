"""Kalshi adapter.

Kalshi (https://kalshi.com) is a CFTC-regulated, real-money exchange. Public
market data is readable without authentication; trading endpoints require an
RSA-signed API key. EdgeRadar only ever READS, so we use the public market feed.

Endpoint: GET {base}/markets?limit=N&status=open
          -> {"cursor": ..., "markets": [ {...}, ... ]}

Price -> implied probability: a Kalshi YES contract settles at $1 if the event
happens, $0 otherwise, so the YES price in dollars IS the implied probability.
We take the midpoint of the YES bid/ask as the fair point estimate:

    implied_prob = (yes_bid + yes_ask) / 2        (both already in dollars, 0..1)

The bid/ask SPREAD is the cost to cross the book; we record it now so Phase 5 can
model the fee + half-spread adjustment. Markets with no quotes (mid == 0) get
implied_prob = None rather than a fabricated value.

Auth note: signed requests are not needed for public reads and are intentionally
omitted in Phase 1. If we ever add authenticated endpoints, the RSA-PSS signing
would slot into `_auth_headers`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from decimal import Decimal

import httpx

from edgeradar.adapters._util import DRY_RUN_TS, parse_iso
from edgeradar.adapters.base import SourceAdapter
from edgeradar.config import get_settings
from edgeradar.fees import trading_cost
from edgeradar.models import MarketQuote, RawRecord


def _price_dollars(market: dict, field: str) -> float | None:
    """Read a Kalshi price field as dollars (0..1).

    Prefers the string `<field>_dollars` representation; falls back to an integer
    cents field `<field>` (older API shape) divided by 100.
    """
    if (raw := market.get(f"{field}_dollars")) is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    if (cents := market.get(field)) is not None:
        try:
            return float(cents) / 100.0
        except (TypeError, ValueError):
            return None
    return None


class KalshiAdapter(SourceAdapter):
    """Fetch and normalize Kalshi binary markets (read-only, public feed)."""

    source = "kalshi"

    def __init__(self, *, limit: int = 200, **kwargs) -> None:
        super().__init__(**kwargs)
        self.limit = limit

    def _auth_headers(self) -> dict[str, str]:
        # Public reads need no auth in Phase 1. Placeholder for future signed calls.
        return {}

    def fetch(self) -> Iterable[RawRecord]:
        snapshot_ts = DRY_RUN_TS if self.dry_run else self.now_utc()
        if self.dry_run:
            data = json.loads((self.sample_dir / "markets.json").read_text())
        else:
            settings = get_settings()
            resp = httpx.get(
                f"{settings.kalshi_api_base}/markets",
                params={"limit": self.limit, "status": "open"},
                headers=self._auth_headers(),
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
        markets = data.get("markets", [])
        return [
            RawRecord(source=self.source, market_id=m["ticker"], snapshot_ts=snapshot_ts, payload=m)
            for m in markets
        ]

    def normalize(self, raw: Iterable[RawRecord]) -> Iterable[MarketQuote]:
        quotes: list[MarketQuote] = []
        for rec in raw:
            m = rec.payload
            if m.get("status") != "active":
                continue
            bid = _price_dollars(m, "yes_bid")
            ask = _price_dollars(m, "yes_ask")
            if bid is None or ask is None:
                continue
            mid = (bid + ask) / 2.0
            implied = mid if 0.0 < mid < 1.0 else None
            spread = max(ask - bid, 0.0)
            last = _price_dollars(m, "last_price")
            quotes.append(
                MarketQuote(
                    source=self.source,
                    market_id=str(m["ticker"]),
                    outcome="YES",
                    title=m.get("title", ""),
                    price=Decimal(str(last if last is not None else mid)),
                    implied_prob=implied,
                    fee_adj_prob=implied,  # fair point estimate = mid
                    spread=spread,
                    trade_cost=trading_cost(self.source, implied, spread),
                    snapshot_ts=rec.snapshot_ts,
                    close_ts=parse_iso(m.get("close_time")),
                )
            )
        return quotes
