"""Phase 5 tests: fee model + weather-edge math."""

from __future__ import annotations

import pytest

from edgeradar.fees import kalshi_fee, trading_cost
from edgeradar.ingest import run_ingest
from edgeradar.weather import (
    build_weather_edge,
    normal_cdf,
    parse_threshold,
    prob_high_above,
)


def test_kalshi_fee_peaks_at_half_and_zero_at_extremes():
    assert kalshi_fee(0.5) == pytest.approx(0.07 * 0.25)
    assert kalshi_fee(0.01) < kalshi_fee(0.5)
    assert kalshi_fee(0.99) < kalshi_fee(0.5)


def test_trading_cost_by_source():
    # Kalshi: half-spread + fee. spread 0.03 -> half 0.015, plus fee at 0.5.
    c = trading_cost("kalshi", 0.5, 0.03)
    assert c == pytest.approx(0.015 + 0.07 * 0.25)
    # Manifold (play money) -> 0.0; None price -> None.
    assert trading_cost("manifold", 0.5, None) == 0.0
    assert trading_cost("kalshi", None, 0.03) is None


def test_normal_cdf_and_prob_above():
    assert normal_cdf(0.0) == pytest.approx(0.5)
    # High 86, threshold 82.5, sigma 4 -> P(above) ~ 0.81.
    p = prob_high_above(86.0, 82.5, 4.0)
    assert 0.80 <= p <= 0.82


def test_parse_threshold():
    assert parse_threshold("Will the high temperature in NYC be above 82.5F?") == ("above", 82.5)
    assert parse_threshold("Will the low be below 40 degrees?") == ("below", 40.0)
    assert parse_threshold("Will the Celtics win?") is None
    # Regression: sports markets must NOT parse as a temperature threshold.
    assert parse_threshold("New York Mets wins by over 1.5 runs") is None
    assert parse_threshold("yes Over 4.5 goals scored") is None


def test_parse_threshold_symbol_form():
    # Found on live data: Kalshi titles use symbols, not words, for many cities
    # ("Will the high temp in NYC be >88° on Jun 19, 2026?").
    assert parse_threshold("Will the high temp in NYC be >88° on Jun 19, 2026?") == ("above", 88.0)
    assert parse_threshold("Will the high temp in Chicago be <74° on Jun 19, 2026?") == (
        "below",
        74.0,
    )
    # A band ("85-86°") is a different market type, not a single threshold —
    # must NOT be approximated as one.
    assert parse_threshold("Will the high temp in NYC be 85-86° on Jun 19, 2026?") is None


def test_fit_sigma_mle_recovers_synthetic():
    # Generate deterministic outcomes from a known sigma and check we recover it.
    import random

    from edgeradar.weather import fit_sigma_mle

    rng = random.Random(0)
    true_sigma = 5.0
    margins, outcomes = [], []
    for _ in range(4000):
        m = rng.uniform(-15, 15)
        p = normal_cdf(m / true_sigma)
        margins.append(m)
        outcomes.append(1 if rng.random() < p else 0)
    fitted = fit_sigma_mle(margins, outcomes)
    assert abs(fitted - true_sigma) < 1.0  # recovered close to the true sigma


def test_is_temperature_market_rejects_sports():
    from edgeradar.weather import is_temperature_market

    assert is_temperature_market("Will the high temperature in NYC be above 82.5F?")
    assert not is_temperature_market("New York Mets wins by over 1.5 runs")
    assert not is_temperature_market("yes Golden State, no Over 4.5 goals scored")


def test_weather_edge_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    from edgeradar.config import get_settings

    get_settings.cache_clear()
    run_ingest("kalshi", dry_run=True)  # lands the NYC temp market

    df = build_weather_edge(data_root=str(tmp_path), dry_run=True)
    assert not df.empty
    row = df[df["market_id"] == "KXHIGHNY-26JUN17-B82.5"].iloc[0]
    # Forecast 86F vs threshold 82.5F -> ~0.81; Kalshi prices 0.465 -> clear edge.
    assert row["forecast_prob"] == pytest.approx(0.81, abs=0.02)
    assert row["edge_net"] > 0
    assert bool(row["is_signal"]) is True


def test_weather_covers_additional_kalshi_cities(tmp_path):
    # Task 5: LOCATIONS was widened beyond NYC so more real Kalshi temperature
    # markets get an NWS comparison. Chicago must now produce a real edge.
    from datetime import datetime, timezone
    from decimal import Decimal

    from edgeradar.models import MarketQuote
    from edgeradar.storage import write_quotes_grouped

    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    quotes = [
        MarketQuote(
            source="kalshi",
            market_id="KXHIGHCHI-EXAMPLE",
            outcome="YES",
            title="Will the high temperature in Chicago be above 75F?",
            price=Decimal("0.50"),
            implied_prob=0.50,
            fee_adj_prob=0.50,
            snapshot_ts=ts,
            close_ts=datetime(2026, 6, 19, tzinfo=timezone.utc),
        )
    ]
    write_quotes_grouped(quotes, data_root=str(tmp_path))
    df = build_weather_edge(data_root=str(tmp_path), dry_run=True)
    assert not df.empty
    row = df.iloc[0]
    assert row["location"] == "CHICAGO"
    # Forecast 78F vs threshold 75F -> a real, non-fabricated probability.
    assert 0.0 < row["forecast_prob"] < 1.0


def test_weather_does_not_fabricate_rows_for_unconfigured_cities(tmp_path):
    # A city not in LOCATIONS (e.g. an international Polymarket-style city —
    # genuinely no NWS overlap) must be skipped, never produce a fabricated row.
    from datetime import datetime, timezone
    from decimal import Decimal

    from edgeradar.models import MarketQuote
    from edgeradar.storage import write_quotes_grouped

    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    quotes = [
        MarketQuote(
            source="kalshi",
            market_id="TOKYO-EXAMPLE",
            outcome="YES",
            title="Will the high temperature in Tokyo be above 30F?",
            price=Decimal("0.50"),
            implied_prob=0.50,
            fee_adj_prob=0.50,
            snapshot_ts=ts,
        )
    ]
    write_quotes_grouped(quotes, data_root=str(tmp_path))
    df = build_weather_edge(data_root=str(tmp_path), dry_run=True)
    assert df.empty
