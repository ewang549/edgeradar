"""Category/keyword targeting for ingestion (Task 2).

Default ingestion pulls "everything" each adapter's API hands back, paged in
whatever order the venue returns it. On live Kalshi data that default order is
dominated by MVE combo baskets (see `adapters/kalshi.py`), so finding markets
that genuinely overlap with other platforms — World Cup winners, a shared BTC
price level, CPI prints, election outcomes, city temperatures — is impractical
by blind pagination alone.

This module maps a handful of named categories to each source's own
server-side filter:

- Kalshi:      `series_ticker` (confirmed on live data to exclude combos
               entirely — Kalshi filters server-side, we don't post-filter).
- Manifold:    `search-markets?term=...` (keyword search).
- Polymarket:  `public-search?q=...` (keyword search).
- The Odds API: the existing `sports` config list (already a targeting knob).

Nothing here invents a market — it only narrows *which real markets* a source
adapter asks the venue for. The series tickers below were confirmed live against
each venue's public API; if a venue retires one, that category simply returns
fewer (or zero) markets rather than erroring — never fabricated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Kalshi series tickers, confirmed live (GET /markets?series_ticker=<X>&status=open
# returns real, non-combo markets — see ARCHITECTURE.md). Some are seasonal/off-cycle
# and may return zero open markets at any given moment; that's expected, not a bug.
CATEGORY_KALSHI_SERIES: dict[str, list[str]] = {
    "world_cup": ["KXMENWORLDCUP", "KXWT20WORLDCUP", "KXCLUBWC"],
    "sports_finals": ["KXNBA", "KXSB", "KXNHL", "KXWNBA"],
    "elections": ["KXIMPEACH", "KXTRUMPAPPROVALYEAR", "KXAIBILL", "PRES", "KXHOUSE"],
    "crypto": ["KXBTCD", "KXBTC", "KXETHD", "KXETH"],
    "macro": ["KXCPI", "KXCPIYOY", "KXFEDDECISION", "KXFED"],
    "weather": [
        "KXHIGHNY",
        "KXHIGHCHI",
        "KXHIGHMIA",
        "KXHIGHDEN",
        "KXHIGHLAX",
        "KXHIGHTPHX",
        "KXHIGHTDAL",
        "KXHIGHTSEA",
        "KXHIGHTATL",
        "KXHIGHTBOS",
        "KXHIGHTSFO",
        "KXHIGHTHOU",
        "KXHIGHPHIL",
        "KXHIGHTLV",
        "KXHIGHTMIN",
        "KXHIGHAUS",
        "KXHIGHTNOLA",
        "KXHIGHTSATX",
        "KXDVHIGH",
    ],
}

# Keyword search terms for Manifold (search-markets?term=) and Polymarket
# (public-search?q=). Kept short and high-precision rather than exhaustive.
CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "world_cup": ["World Cup"],
    "sports_finals": ["NBA Finals", "Super Bowl", "Stanley Cup"],
    "elections": ["election", "president"],
    "crypto": ["Bitcoin", "Ethereum"],
    "macro": ["CPI", "Fed rate"],
    "weather": ["temperature"],
}

# The Odds API already targets via `sports` (its own config knob); map our
# categories onto its sport keys for a consistent --categories experience.
CATEGORY_ODDSAPI_SPORTS: dict[str, list[str]] = {
    "world_cup": ["soccer_fifa_world_cup"],
    "sports_finals": ["basketball_nba", "americanfootball_nfl", "icehockey_nhl"],
}

KNOWN_CATEGORIES = sorted(
    set(CATEGORY_KALSHI_SERIES) | set(CATEGORY_KEYWORDS) | set(CATEGORY_ODDSAPI_SPORTS)
)


@dataclass(frozen=True)
class Targeting:
    """Resolved per-source targeting params for a set of category names."""

    categories: list[str]
    kalshi_series: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    oddsapi_sports: list[str] = field(default_factory=list)


def resolve_categories(categories: list[str] | None) -> Targeting:
    """Resolve named categories into concrete per-source filter params.

    Unknown category names are ignored (never raise — a typo in a CLI flag
    shouldn't crash ingestion; it just targets nothing extra for that name).
    """
    cats = [c.strip().lower() for c in (categories or []) if c.strip()]
    series: list[str] = []
    keywords: list[str] = []
    sports: list[str] = []
    for c in cats:
        series.extend(CATEGORY_KALSHI_SERIES.get(c, []))
        keywords.extend(CATEGORY_KEYWORDS.get(c, []))
        sports.extend(CATEGORY_ODDSAPI_SPORTS.get(c, []))
    # de-dupe, preserve order
    return Targeting(
        categories=cats,
        kalshi_series=list(dict.fromkeys(series)),
        keywords=list(dict.fromkeys(keywords)),
        oddsapi_sports=list(dict.fromkeys(sports)),
    )
