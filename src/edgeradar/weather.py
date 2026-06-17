"""Weather-edge module: NWS forecasts vs Kalshi daily-temperature markets.

Kalshi runs daily markets like "Will the high temperature in NYC be above 82.5F?".
The National Weather Service (api.weather.gov, free, no key) publishes the official
forecast high. If we turn the forecast into a probability for the same threshold,
we can compare it to Kalshi's price and flag gaps.

Turning a forecast into a probability — the simple, defensible model:
a point forecast high `H` is treated as the mean of a Normal distribution with a
day-ahead standard deviation `sigma` (forecast highs are off by a few degrees), so

    P(high > threshold) = 1 - Phi((threshold - H) / sigma)

This is intentionally simple and is exactly the kind of assumption Phase 6 will
score against real outcomes (are these forecast-implied probabilities actually
well-calibrated?). The edge is reported net of Kalshi's trading cost.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import httpx
import pandas as pd

from edgeradar.config import get_settings
from edgeradar.storage import read_quotes

# Configured locations: NWS forecast endpoint + how to spot the city in a market
# title + a day-ahead sigma (deg F). Add rows here to cover more cities.
LOCATIONS: dict[str, dict] = {
    "NYC": {
        # NWS grid IDs aren't stable, so we store lat/lon and look up the correct
        # forecast endpoint at runtime via /points/{lat},{lon} (Central Park here).
        "lat": 40.7790,
        "lon": -73.9692,
        "city_tokens": ("nyc", "new york"),
        "sigma": 4.0,
    },
}

# The number must be followed by a temperature unit (°, F, or "degrees") so we don't
# mistake "wins by over 1.5 runs" or "Over 4.5 goals" for a temperature threshold.
_THRESHOLD_RE = re.compile(
    r"(above|over|greater than|at least|below|under|less than)"
    r"\s+(\d+(?:\.\d+)?)\s*(?:°|degrees?|f)\b",
    re.IGNORECASE,
)
_ABOVE_WORDS = {"above", "over", "greater than", "at least"}
# A second guard: the title must actually talk about temperature.
_TEMP_KEYWORDS = ("temperature", "temp", "degrees", "°")


def is_temperature_market(title: str) -> bool:
    """True only for genuine temperature markets (guards against sports/other markets)."""
    t = title.lower()
    return any(kw in t for kw in _TEMP_KEYWORDS)


@dataclass
class Forecast:
    location: str
    date: str  # ISO date (YYYY-MM-DD)
    high_f: float
    sigma: float


def normal_cdf(z: float) -> float:
    """Standard normal CDF via the error function."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def prob_high_above(high_f: float, threshold: float, sigma: float) -> float:
    """P(daily high > threshold) under Normal(high_f, sigma)."""
    if sigma <= 0:
        return 1.0 if high_f > threshold else 0.0
    return 1.0 - normal_cdf((threshold - high_f) / sigma)


def parse_threshold(title: str) -> tuple[str, float] | None:
    """Extract ('above'|'below', value) from a temperature market title, or None."""
    m = _THRESHOLD_RE.search(title)
    if not m:
        return None
    direction = "above" if m.group(1).lower() in _ABOVE_WORDS else "below"
    return direction, float(m.group(2))


def _match_location(title: str) -> str | None:
    t = title.lower()
    for loc, cfg in LOCATIONS.items():
        if any(tok in t for tok in cfg["city_tokens"]):
            return loc
    return None


def fetch_forecasts(location: str, *, dry_run: bool = False) -> list[Forecast]:
    """Daytime-high forecasts for a configured location (offline in dry-run)."""
    cfg = LOCATIONS[location]
    if dry_run:
        sample = Path("sample_responses/weather") / f"{location.lower()}_forecast.json"
        data = json.loads(sample.read_text())
    else:
        settings = get_settings()
        headers = {"User-Agent": settings.nws_user_agent, "Accept": "application/geo+json"}
        try:
            # 1) Resolve lat/lon to the correct (current) forecast URL for this grid.
            pts = httpx.get(
                f"{settings.nws_api_base}/points/{cfg['lat']},{cfg['lon']}",
                headers=headers,
                timeout=30.0,
            )
            pts.raise_for_status()
            forecast_url = pts.json()["properties"]["forecast"]
            # 2) Fetch the actual forecast.
            resp = httpx.get(forecast_url, headers=headers, timeout=30.0)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            # Never let a weather hiccup crash the pipeline; just skip this location.
            print(f"[weather] NWS fetch failed for {location} ({exc}); skipping.")
            return []

    out: list[Forecast] = []
    for period in data.get("properties", {}).get("periods", []):
        if not period.get("isDaytime"):
            continue
        date = str(period["startTime"])[:10]
        out.append(
            Forecast(
                location=location,
                date=date,
                high_f=float(period["temperature"]),
                sigma=cfg["sigma"],
            )
        )
    return out


def build_weather_edge(*, data_root: str | None = None, dry_run: bool = False) -> pd.DataFrame:
    """Join NWS forecasts to Kalshi temperature markets; compute fee-aware edge.

    Writes data/marts/weather_edge.parquet and returns the DataFrame.
    """
    settings = get_settings()
    root = data_root or settings.data_root

    quotes = read_quotes(source="kalshi", data_root=root)
    rows: list[dict] = []
    if not quotes.empty:
        quotes = quotes[quotes["implied_prob"].notna()].copy()
        # Cache forecasts per location so we fetch each endpoint once.
        forecast_cache: dict[str, dict[str, Forecast]] = {}

        for _, q in quotes.iterrows():
            title = str(q["title"])
            if not is_temperature_market(title):
                continue  # skip non-temperature markets (sports, etc.)
            parsed = parse_threshold(title)
            location = _match_location(title)
            if parsed is None or location is None:
                continue  # not a recognizable temperature market
            direction, threshold = parsed

            if location not in forecast_cache:
                forecast_cache[location] = {
                    f.date: f for f in fetch_forecasts(location, dry_run=dry_run)
                }
            by_date = forecast_cache[location]
            if not by_date:
                continue

            close_date = (
                str(pd.to_datetime(q["close_ts"], utc=True).date())
                if pd.notna(q["close_ts"])
                else None
            )
            fc = by_date.get(close_date) or next(iter(by_date.values()))

            p_above = prob_high_above(fc.high_f, threshold, fc.sigma)
            forecast_prob = p_above if direction == "above" else 1.0 - p_above
            kalshi_prob = float(q["implied_prob"])
            trade_cost = float(q["trade_cost"]) if pd.notna(q.get("trade_cost")) else 0.0
            edge_gross = abs(forecast_prob - kalshi_prob)

            rows.append(
                {
                    "location": location,
                    "market_id": q["market_id"],
                    "title": title,
                    "direction": direction,
                    "threshold_f": threshold,
                    "forecast_date": fc.date,
                    "forecast_high_f": fc.high_f,
                    "sigma_f": fc.sigma,
                    "forecast_prob": round(forecast_prob, 4),
                    "kalshi_prob": round(kalshi_prob, 4),
                    "trade_cost": round(trade_cost, 4),
                    "edge_gross": round(edge_gross, 4),
                    "edge_net": round(edge_gross - trade_cost, 4),
                    "is_signal": bool(edge_gross - trade_cost > 0),
                }
            )

    df = pd.DataFrame(rows)
    out_dir = Path(root) / "marts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "weather_edge.parquet"
    # Write even when empty (stable schema) so dbt can always read it.
    if df.empty:
        df = pd.DataFrame(
            columns=[
                "location",
                "market_id",
                "title",
                "direction",
                "threshold_f",
                "forecast_date",
                "forecast_high_f",
                "sigma_f",
                "forecast_prob",
                "kalshi_prob",
                "trade_cost",
                "edge_gross",
                "edge_net",
                "is_signal",
            ]
        )
    df.to_parquet(out_path, index=False)
    return df
