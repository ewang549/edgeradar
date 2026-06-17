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
    assert parse_threshold("Will the low be below 40F?") == ("below", 40.0)
    assert parse_threshold("Will the Celtics win?") is None


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
