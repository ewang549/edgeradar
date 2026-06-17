"""The Odds API adapter — sportsbook lines as a sharp, data-only consensus source.

The Odds API (https://the-odds-api.com) aggregates bookmaker odds across many
sports (NBA, MLB, NFL, NHL, EPL, ...). Sportsbook lines are a *sharp* reference:
comparing a prediction market to the book's vig-removed probability is a much
stronger signal than market-vs-play-money. EdgeRadar treats it as read-only
consensus (never tradeable PnL).

Endpoint: GET {base}/sports/{sport}/odds?apiKey=..&regions=us&markets=h2h&oddsFormat=decimal
          -> events with bookmakers[].markets[].outcomes[] = {name, price(decimal)}

Price -> implied probability: decimal odds `d` imply `1/d`. We average across
bookmakers per outcome, then remove the vig by normalizing the outcomes so they sum
to 1 (proportional method). The free tier is ~500 req/mo, so we make one call per
sport and cache the raw responses in the lake.
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


class OddsApiAdapter(SourceAdapter):
    """Fetch h2h sportsbook odds across sports; emit vig-removed consensus probs."""

    source = "oddsapi"

    def __init__(self, *, sports: str | None = None, regions: str | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        settings = get_settings()
        self.sports = [
            s.strip() for s in (sports or settings.odds_api_sports).split(",") if s.strip()
        ]
        self.regions = regions or settings.odds_api_regions

    def fetch(self) -> Iterable[RawRecord]:
        snapshot_ts = DRY_RUN_TS if self.dry_run else self.now_utc()
        if self.dry_run:
            events = json.loads((self.sample_dir / "odds.json").read_text())
        else:
            settings = get_settings()
            if not settings.odds_api_key:
                print("[oddsapi] no ODDS_API_KEY set; skipping.")
                return []
            events = []
            for sport in self.sports:
                try:
                    resp = httpx.get(
                        f"{settings.odds_api_base}/sports/{sport}/odds",
                        params={
                            "apiKey": settings.odds_api_key,
                            "regions": self.regions,
                            "markets": "h2h",
                            "oddsFormat": "decimal",
                        },
                        timeout=30.0,
                    )
                    resp.raise_for_status()
                    events.extend(resp.json())
                except httpx.HTTPError as exc:
                    print(f"[oddsapi] fetch failed for {sport} ({exc}); skipping that sport.")
                    continue
        return [
            RawRecord(
                source=self.source, market_id=str(ev["id"]), snapshot_ts=snapshot_ts, payload=ev
            )
            for ev in events
        ]

    def normalize(self, raw: Iterable[RawRecord]) -> Iterable[MarketQuote]:
        quotes: list[MarketQuote] = []
        for rec in raw:
            ev = rec.payload
            # Average implied probability per outcome across all bookmakers.
            implied_by_outcome: dict[str, list[float]] = {}
            for book in ev.get("bookmakers", []):
                for market in book.get("markets", []):
                    if market.get("key") != "h2h":
                        continue
                    for oc in market.get("outcomes", []):
                        price = oc.get("price")
                        if not price or float(price) <= 1.0:
                            continue
                        implied_by_outcome.setdefault(oc["name"], []).append(1.0 / float(price))
            if len(implied_by_outcome) < 2:
                continue
            means = {name: sum(v) / len(v) for name, v in implied_by_outcome.items()}
            total = sum(means.values())  # > 1 due to vig
            if total <= 0:
                continue

            close_ts = parse_iso(ev.get("commence_time"))
            home, away = ev.get("home_team"), ev.get("away_team")
            sport = ev.get("sport_title", "")
            for name, mean_implied in means.items():
                fair = mean_implied / total  # proportional vig removal
                implied = fair if 0.0 < fair < 1.0 else None
                opponent = away if name == home else home
                title = f"{name} to win vs {opponent} ({sport})"
                quotes.append(
                    MarketQuote(
                        source=self.source,
                        market_id=f"{ev['id']}:{name}",
                        outcome="YES",
                        title=title,
                        price=Decimal(str(round(fair, 4))),
                        implied_prob=implied,
                        fee_adj_prob=implied,
                        spread=None,
                        trade_cost=trading_cost(self.source, implied, None),  # 0.0 (not traded)
                        snapshot_ts=rec.snapshot_ts,
                        close_ts=close_ts,
                    )
                )
        return quotes
