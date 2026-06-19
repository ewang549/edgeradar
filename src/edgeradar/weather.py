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
#
# Widened from NYC-only to the Kalshi weather series found on live data
# (KXHIGHCHI, KXHIGHDEN, KXHIGHMIA, ...) so more real Kalshi temperature markets
# get an NWS-forecast comparison. NWS (api.weather.gov) only covers the US, so
# this does NOT create overlap with Polymarket's international temperature
# markets (Tokyo/Beijing/Cape Town/... — confirmed on live data, see
# FINDINGS.md); that non-overlap is real and stays unfabricated.
LOCATIONS: dict[str, dict] = {
    "NYC": {
        # NWS grid IDs aren't stable, so we store lat/lon and look up the correct
        # forecast endpoint at runtime via /points/{lat},{lon} (Central Park here).
        "lat": 40.7790,
        "lon": -73.9692,
        "city_tokens": ("nyc", "new york"),
        "sigma": 4.0,
    },
    "CHICAGO": {"lat": 41.8781, "lon": -87.6298, "city_tokens": ("chicago",), "sigma": 4.0},
    "DENVER": {"lat": 39.7392, "lon": -104.9903, "city_tokens": ("denver",), "sigma": 4.0},
    "MIAMI": {"lat": 25.7617, "lon": -80.1918, "city_tokens": ("miami",), "sigma": 4.0},
    "LOS_ANGELES": {
        "lat": 34.0522,
        "lon": -118.2437,
        # "la" is safe here ONLY because `_match_location` uses word-boundary
        # matching (not substring) — a naive substring check would also fire
        # inside unrelated words like "Atlanta".
        "city_tokens": ("los angeles", "la"),
        "sigma": 4.0,
    },
    "PHOENIX": {"lat": 33.4484, "lon": -112.0740, "city_tokens": ("phoenix",), "sigma": 4.0},
    "DALLAS": {"lat": 32.7767, "lon": -96.7970, "city_tokens": ("dallas",), "sigma": 4.0},
    "SEATTLE": {"lat": 47.6062, "lon": -122.3321, "city_tokens": ("seattle",), "sigma": 4.0},
    "ATLANTA": {"lat": 33.7490, "lon": -84.3880, "city_tokens": ("atlanta",), "sigma": 4.0},
    "BOSTON": {"lat": 42.3601, "lon": -71.0589, "city_tokens": ("boston",), "sigma": 4.0},
    "SAN_FRANCISCO": {
        "lat": 37.7749,
        "lon": -122.4194,
        "city_tokens": ("san francisco",),
        "sigma": 4.0,
    },
    "HOUSTON": {"lat": 29.7604, "lon": -95.3698, "city_tokens": ("houston",), "sigma": 4.0},
    "PHILADELPHIA": {
        "lat": 39.9526,
        "lon": -75.1652,
        "city_tokens": ("philadelphia",),
        "sigma": 4.0,
    },
    "LAS_VEGAS": {"lat": 36.1699, "lon": -115.1398, "city_tokens": ("las vegas",), "sigma": 4.0},
    "MINNEAPOLIS": {
        "lat": 44.9778,
        "lon": -93.2650,
        "city_tokens": ("minneapolis",),
        "sigma": 4.0,
    },
    "AUSTIN": {"lat": 30.2672, "lon": -97.7431, "city_tokens": ("austin",), "sigma": 4.0},
    "NEW_ORLEANS": {
        "lat": 29.9511,
        "lon": -90.0715,
        "city_tokens": ("new orleans",),
        "sigma": 4.0,
    },
    "SAN_ANTONIO": {
        "lat": 29.4241,
        "lon": -98.4936,
        "city_tokens": ("san antonio",),
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
# Live Kalshi titles also use symbol form ("be >88° on...", "be <81° on...")
# instead of words — found on live data, see FINDINGS.md. Titles expressing a
# narrow band ("85-86°") are a genuinely different market type (not a single
# threshold) and are deliberately left unparsed rather than approximated.
_SYMBOL_THRESHOLD_RE = re.compile(r"(>=|≥|>|<=|≤|<)\s*(\d+(?:\.\d+)?)\s*°", re.IGNORECASE)
_SYMBOL_ABOVE = {">", ">=", "≥"}
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
    """Extract ('above'|'below', value) from a temperature market title, or None.

    Handles both word form ("above 82.5F") and symbol form (">88°", "<81°").
    A band-style title ("85-86°") matches neither and correctly returns None —
    that's a different market type, not a threshold to approximate.
    """
    m = _THRESHOLD_RE.search(title)
    if m:
        direction = "above" if m.group(1).lower() in _ABOVE_WORDS else "below"
        return direction, float(m.group(2))
    m2 = _SYMBOL_THRESHOLD_RE.search(title)
    if m2:
        direction = "above" if m2.group(1) in _SYMBOL_ABOVE else "below"
        return direction, float(m2.group(2))
    return None


def _match_location(title: str) -> str | None:
    # Word-boundary matching (not naive substring) so a short token like "la"
    # can't false-positive inside an unrelated word (e.g. "Atlanta" contains "la").
    t = title.lower()
    for loc, cfg in LOCATIONS.items():
        if any(re.search(rf"\b{re.escape(tok)}\b", t) for tok in cfg["city_tokens"]):
            return loc
    return None


def fetch_forecasts(location: str, *, dry_run: bool = False, sigma: float = 4.0) -> list[Forecast]:
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
                sigma=sigma,
            )
        )
    return out


def build_weather_edge(*, data_root: str | None = None, dry_run: bool = False) -> pd.DataFrame:
    """Join NWS forecasts to Kalshi temperature markets; compute fee-aware edge.

    Writes data/marts/weather_edge.parquet and returns the DataFrame.
    """
    settings = get_settings()
    root = data_root or settings.data_root
    sigma = sigma_for(root)

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
                    f.date: f for f in fetch_forecasts(location, dry_run=dry_run, sigma=sigma)
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


# --- sigma calibration -------------------------------------------------------
# The Normal model needs a day-ahead forecast-uncertainty sigma. We start from a
# prior (config `weather_sigma_f`, default 4F) and refine it from resolved outcomes:
# given many (forecast_high, threshold, did-it-happen) records, pick the sigma that
# best explains the outcomes (maximum likelihood). The fitted value is written to
# data/marts/weather_sigma.json and picked up by future runs.

_SIGMA_FILE = "weather_sigma.json"
_MIN_CALIBRATION_SAMPLES = 15


def sigma_for(data_root: str | None = None) -> float:
    """Return the calibrated sigma if one has been fitted, else the configured prior."""
    settings = get_settings()
    root = data_root or settings.data_root
    path = Path(root) / "marts" / _SIGMA_FILE
    if path.exists():
        try:
            return float(json.loads(path.read_text())["sigma"])
        except (ValueError, KeyError, OSError):
            pass
    return settings.weather_sigma_f


def fit_sigma_mle(margins: list[float], outcomes: list[int]) -> float:
    """Maximum-likelihood sigma for P(yes) = Phi(margin / sigma) given binary outcomes.

    `margin` is (forecast_high - threshold) oriented so positive favors "yes".
    Coarse-then-fine 1-D search; no SciPy dependency.
    """
    eps = 1e-6

    def neg_log_likelihood(sigma: float) -> float:
        total = 0.0
        for m, y in zip(margins, outcomes, strict=True):
            p = min(max(normal_cdf(m / sigma), eps), 1 - eps)
            total -= y * math.log(p) + (1 - y) * math.log(1 - p)
        return total

    best, best_nll = 4.0, float("inf")
    # coarse grid then refine around the best point
    grid = [0.5 + 0.25 * i for i in range(0, 78)]  # 0.5 .. ~20
    for s in grid:
        nll = neg_log_likelihood(s)
        if nll < best_nll:
            best, best_nll = s, nll
    fine = [best - 0.25 + 0.02 * i for i in range(0, 26) if best - 0.25 + 0.02 * i > 0]
    for s in fine:
        nll = neg_log_likelihood(s)
        if nll < best_nll:
            best, best_nll = s, nll
    return round(best, 3)


def calibrate_weather_sigma(
    *, data_root: str | None = None, resolutions_path: str = "seeds/resolutions.csv"
) -> tuple[float, int, bool]:
    """Fit sigma from resolved temperature markets and persist it. Returns (sigma, n, written).

    Joins weather_edge (forecast_high_f, threshold_f, direction) to known outcomes.
    Needs at least a handful of resolved markets; otherwise keeps the current prior.
    """
    settings = get_settings()
    root = data_root or settings.data_root
    edge_path = Path(root) / "marts" / "weather_edge.parquet"
    res_path = Path(resolutions_path)
    if not edge_path.exists() or not res_path.exists():
        return sigma_for(root), 0, False

    edge = pd.read_parquet(edge_path)
    res = pd.read_csv(res_path)[["market_id", "outcome"]].dropna()
    res["outcome"] = res["outcome"].astype(int)
    joined = edge.merge(res, on="market_id", how="inner")
    if joined.empty:
        return sigma_for(root), 0, False

    margins, outcomes = [], []
    for _, r in joined.iterrows():
        high, thr = float(r["forecast_high_f"]), float(r["threshold_f"])
        margin = (high - thr) if r["direction"] == "above" else (thr - high)
        margins.append(margin)
        outcomes.append(int(r["outcome"]))

    n = len(margins)
    if n < _MIN_CALIBRATION_SAMPLES:
        return sigma_for(root), n, False  # not enough data yet — keep the prior

    sigma = fit_sigma_mle(margins, outcomes)
    out_dir = Path(root) / "marts"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / _SIGMA_FILE).write_text(json.dumps({"sigma": sigma, "n": n}))
    return sigma, n, True
