"""Polymarket adapter (data-only consensus signal).

Polymarket is a real-money (crypto) prediction market, but US users generally
cannot trade there, so EdgeRadar treats it as a READ-ONLY consensus reference —
it informs the cross-platform consensus but never counts toward tradeable PnL
(only Kalshi does; see evaluation.py).

Endpoint: GET {base}/markets?closed=false&order=volume&ascending=false&limit=N
          -> a JSON array of market objects.

Price -> implied probability: a binary market exposes `outcomes` ("[\"Yes\",\"No\"]")
and `outcomePrices` ("[\"0.97\",\"0.03\"]"), where each share pays $1 if it occurs,
so the "Yes" price IS the implied probability. We also read bestBid/bestAsk for the
spread (recorded for completeness; Polymarket trade_cost is treated as 0 since we
don't trade it).
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


def _json_list(raw: str | list | None) -> list:
    """Polymarket encodes some array fields as JSON strings; decode defensively."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []


class PolymarketAdapter(SourceAdapter):
    """Fetch and normalize Polymarket binary markets (read-only consensus)."""

    source = "polymarket"

    def __init__(self, *, limit: int = 200, **kwargs) -> None:
        super().__init__(**kwargs)
        self.limit = limit

    def fetch(self) -> Iterable[RawRecord]:
        snapshot_ts = DRY_RUN_TS if self.dry_run else self.now_utc()
        if self.dry_run:
            data = json.loads((self.sample_dir / "markets.json").read_text())
        else:
            settings = get_settings()
            resp = httpx.get(
                f"{settings.polymarket_api_base}/markets",
                params={
                    "closed": "false",
                    "order": "volume",
                    "ascending": "false",
                    "limit": self.limit,
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
        return [
            RawRecord(
                source=self.source, market_id=str(m["id"]), snapshot_ts=snapshot_ts, payload=m
            )
            for m in data
        ]

    def normalize(self, raw: Iterable[RawRecord]) -> Iterable[MarketQuote]:
        quotes: list[MarketQuote] = []
        for rec in raw:
            m = rec.payload
            if m.get("closed") or not m.get("active", True):
                continue
            outcomes = [str(o).strip().lower() for o in _json_list(m.get("outcomes"))]
            prices = _json_list(m.get("outcomePrices"))
            # Only handle binary Yes/No markets.
            if outcomes[:2] != ["yes", "no"] or len(prices) < 1:
                continue
            try:
                p_yes = float(prices[0])
            except (TypeError, ValueError):
                continue
            implied = p_yes if 0.0 < p_yes < 1.0 else None
            spread = None
            if m.get("bestAsk") is not None and m.get("bestBid") is not None:
                spread = max(float(m["bestAsk"]) - float(m["bestBid"]), 0.0)
            quotes.append(
                MarketQuote(
                    source=self.source,
                    market_id=str(m["id"]),
                    outcome="YES",
                    title=m.get("question", ""),
                    price=Decimal(str(p_yes)),
                    implied_prob=implied,
                    fee_adj_prob=implied,
                    spread=spread,
                    trade_cost=trading_cost(self.source, implied, spread),  # 0.0 (not traded)
                    snapshot_ts=rec.snapshot_ts,
                    close_ts=parse_iso(m.get("endDate")),
                )
            )
        return quotes
