"""Manifold adapter.

Manifold (https://manifold.markets) is a play-money prediction market with a
simple, no-auth public API. We use it as a broad consensus signal.

Endpoint: GET {base}/markets?limit=N  -> a JSON array of market objects.

Price -> implied probability: for a BINARY market Manifold already exposes
`probability` in (0,1), which IS the market-implied probability — no odds
conversion or vig removal needed (a single CPMM pool, not a two-sided book).
Because it's play money, there are effectively no trading fees; fee adjustment
is left to Phase 5 (kept as None here so we don't fabricate a number).
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

import httpx

from edgeradar.adapters._util import DRY_RUN_TS, ms_to_dt
from edgeradar.adapters.base import SourceAdapter
from edgeradar.config import get_settings
from edgeradar.fees import trading_cost
from edgeradar.models import MarketQuote, RawRecord


class ManifoldAdapter(SourceAdapter):
    """Fetch and normalize Manifold binary markets."""

    source = "manifold"

    def __init__(self, *, limit: int = 200, **kwargs) -> None:
        super().__init__(**kwargs)
        self.limit = limit

    def fetch(self) -> Iterable[RawRecord]:
        snapshot_ts = DRY_RUN_TS if self.dry_run else self.now_utc()
        if self.dry_run:
            import json

            data = json.loads((self.sample_dir / "markets.json").read_text())
        else:
            settings = get_settings()
            resp = httpx.get(
                f"{settings.manifold_api_base}/markets",
                params={"limit": self.limit},
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
        return [
            RawRecord(source=self.source, market_id=m["id"], snapshot_ts=snapshot_ts, payload=m)
            for m in data
        ]

    def normalize(self, raw: Iterable[RawRecord]) -> Iterable[MarketQuote]:
        quotes: list[MarketQuote] = []
        for rec in raw:
            m = rec.payload
            # Only tradeable, unresolved binary markets carry a usable probability.
            if m.get("outcomeType") != "BINARY" or m.get("isResolved"):
                continue
            prob = m.get("probability")
            if prob is None:
                continue
            implied = float(prob) if 0.0 < float(prob) < 1.0 else None
            quotes.append(
                MarketQuote(
                    source=self.source,
                    market_id=str(m["id"]),
                    outcome="YES",
                    title=m.get("question", ""),
                    price=Decimal(str(prob)),
                    implied_prob=implied,
                    fee_adj_prob=implied,  # play money: fair estimate = quoted prob
                    spread=None,  # single CPMM pool, no two-sided book
                    trade_cost=trading_cost(self.source, implied, None),  # 0.0 (play money)
                    snapshot_ts=rec.snapshot_ts,
                    close_ts=ms_to_dt(m.get("closeTime")),
                )
            )
        return quotes
