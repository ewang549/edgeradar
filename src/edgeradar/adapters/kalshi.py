"""Kalshi adapter.

Kalshi (https://kalshi.com) is a CFTC-regulated, real-money exchange. Public
market data is readable without authentication; trading endpoints require an
RSA-signed API key. EdgeRadar only ever READS, so we use the public market feed.

Endpoint: GET {base}/markets?limit=N&status=open[&cursor=...][&series_ticker=...]
          -> {"cursor": ..., "markets": [ {...}, ... ]}

Price -> implied probability: a Kalshi YES contract settles at $1 if the event
happens, $0 otherwise, so the YES price in dollars IS the implied probability.
We take the midpoint of the YES bid/ask as the fair point estimate:

    implied_prob = (yes_bid + yes_ask) / 2        (both already in dollars, 0..1)

The bid/ask SPREAD is the cost to cross the book; we record it now so Phase 5 can
model the fee + half-spread adjustment. Markets with no quotes (mid == 0) get
implied_prob = None rather than a fabricated value.

GOTCHA FOUND ON LIVE DATA — MVE combo/parlay baskets crowd out real markets:
a plain `GET /markets?status=open&limit=200` call returns Kalshi's "Multi-Variable
Event" (MVE) combo/parlay products first — e.g. ``KXMVESPORTSMULTIGAMEEXTENDED``,
which bundles ~8 unrelated games into one basket. On a live pull, *all 200* of the
first markets returned were MVE combos: illiquid (no real bid/ask), with a `title`
that is a concatenated leg list ("yes Brazil,yes USA,yes Ecuador,..."), un-matchable
against any other platform's wording. Confusingly, these report
`market_type: "binary"` exactly like a normal single-outcome market — `market_type`
does NOT distinguish them. The real signal is `mve_collection_ticker` (set on every
combo leg-product), `mve_selected_legs` (the bundled legs), or the `KXMVE...` ticker
prefix. We exclude all three. See FINDINGS.md for the live-data writeup.

Because combos vastly outnumber normal markets in the default feed order (a live
sample found ~1 normal market per ~500 combo rows), blind pagination is an
impractical way to find overlap-worthy markets. `series_tickers` (wired from
`edgeradar.targeting`) lets a caller fetch a specific, real series — e.g.
`series_ticker=KXHIGHNY` — which Kalshi's API filters server-side with no combos
mixed in at all (confirmed on live data).
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

# Ticker prefixes for Kalshi's Multi-Variable-Event combo/parlay products.
# `KXMVE*` is the one observed on live data; kept as a tuple so a second family can
# be added without touching the filtering logic.
COMBO_TICKER_PREFIXES: tuple[str, ...] = ("KXMVE",)


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


def _num(market: dict, field: str) -> float:
    """Read a numeric field defensively; missing/unparsable -> 0.0 (never fabricated)."""
    raw = market.get(field)
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def is_combo_market(m: dict) -> bool:
    """True for an MVE combo/parlay basket (see module docstring for the gotcha).

    `market_type` is NOT a reliable signal — combos report "binary" just like a
    normal market. The real tells are `mve_collection_ticker`, `mve_selected_legs`,
    or a `KXMVE...` ticker prefix.
    """
    if m.get("mve_collection_ticker"):
        return True
    if m.get("mve_selected_legs"):
        return True
    ticker = str(m.get("ticker", ""))
    return any(ticker.startswith(p) for p in COMBO_TICKER_PREFIXES)


def has_liquidity_signal(m: dict, *, min_dollars: float) -> bool:
    """Non-trivial liquidity/volume/open-interest — the "is this a real market" check."""
    return (
        _num(m, "liquidity_dollars") >= min_dollars
        or _num(m, "volume_24h_fp") >= min_dollars
        or _num(m, "open_interest_fp") >= min_dollars
    )


class KalshiAdapter(SourceAdapter):
    """Fetch and normalize Kalshi binary markets (read-only, public feed)."""

    source = "kalshi"

    def __init__(
        self,
        *,
        limit: int = 200,
        max_pages: int | None = None,
        series_tickers: list[str] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.limit = limit
        # `series_tickers`, when given, targets specific real series (e.g. World Cup,
        # CPI, BTC) instead of the noisy default feed — see edgeradar.targeting.
        self.series_tickers = series_tickers
        settings = get_settings()
        self.max_pages = max_pages if max_pages is not None else settings.kalshi_max_pages

    def _auth_headers(self) -> dict[str, str]:
        # Public reads need no auth in Phase 1. Placeholder for future signed calls.
        return {}

    def _fetch_pages(self, params: dict) -> list[dict]:
        """Paginate /markets (following `cursor`) up to `self.max_pages`."""
        settings = get_settings()
        out: list[dict] = []
        cursor: str | None = None
        for _ in range(max(1, self.max_pages)):
            page_params = dict(params)
            if cursor:
                page_params["cursor"] = cursor
            resp = httpx.get(
                f"{settings.kalshi_api_base}/markets",
                params=page_params,
                headers=self._auth_headers(),
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            markets = data.get("markets", [])
            out.extend(markets)
            cursor = data.get("cursor")
            if not cursor or not markets:
                break
        return out

    def fetch(self) -> Iterable[RawRecord]:
        snapshot_ts = DRY_RUN_TS if self.dry_run else self.now_utc()
        if self.dry_run:
            data = json.loads((self.sample_dir / "markets.json").read_text())
            markets = data.get("markets", [])
        elif self.series_tickers:
            markets = []
            for series in self.series_tickers:
                params = {"limit": self.limit, "status": "open", "series_ticker": series}
                markets.extend(self._fetch_pages(params))
        else:
            markets = self._fetch_pages({"limit": self.limit, "status": "open"})
        return [
            RawRecord(source=self.source, market_id=m["ticker"], snapshot_ts=snapshot_ts, payload=m)
            for m in markets
        ]

    def normalize(self, raw: Iterable[RawRecord]) -> Iterable[MarketQuote]:
        settings = get_settings()
        min_liquidity = settings.kalshi_min_liquidity_dollars
        quotes: list[MarketQuote] = []
        for rec in raw:
            m = rec.payload
            if m.get("status") != "active":
                continue
            if is_combo_market(m):
                continue  # MVE combo/parlay basket — see module docstring

            bid = _price_dollars(m, "yes_bid")
            ask = _price_dollars(m, "yes_ask")
            mid = (bid + ask) / 2.0 if bid is not None and ask is not None else None
            has_live_quote = mid is not None and 0.0 < mid < 1.0 and ask >= bid

            last = _price_dollars(m, "last_price")
            liquid = has_liquidity_signal(m, min_dollars=min_liquidity)

            if has_live_quote:
                implied = mid
                price_is_stale = False
            elif liquid and last is not None and 0.0 < last < 1.0:
                # No live two-sided quote, but a recent trade exists and the market
                # has real volume/liquidity behind it — usable, but flagged so
                # quality.py/the dashboard can see we fell back to a stale price.
                implied = last
                price_is_stale = True
            else:
                # Neither a live quote nor a trustworthy stale fallback: too thin to
                # report rather than fabricate a number.
                continue

            spread = max(ask - bid, 0.0) if bid is not None and ask is not None else None
            quotes.append(
                MarketQuote(
                    source=self.source,
                    market_id=str(m["ticker"]),
                    outcome="YES",
                    title=m.get("title", ""),
                    price=Decimal(str(implied)),
                    implied_prob=implied,
                    fee_adj_prob=implied,  # fair point estimate = mid (or stale fallback)
                    spread=spread,
                    trade_cost=trading_cost(self.source, implied, spread),
                    price_is_stale=price_is_stale,
                    snapshot_ts=rec.snapshot_ts,
                    close_ts=parse_iso(m.get("close_time")),
                )
            )
        return quotes
