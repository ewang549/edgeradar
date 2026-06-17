"""Phase 7 tests: alerter selection/formatting, read-only guard, dashboard compiles."""

from __future__ import annotations

import py_compile
from pathlib import Path

import duckdb
import pytest

from edgeradar.alerter import format_message, load_alertable, run_alert


def _con_with_marts():
    con = duckdb.connect(":memory:")
    con.sql(
        """
        create table mart_divergence as select * from (values
            ('Celtics vs Lakers','kalshi',0.91,0.80,0.11,0.02,0.09,true,'rich'),
            ('Tiny gap','kalshi',0.50,0.49,0.01,0.02,-0.01,false,'rich')
        ) t(
            canonical_title, source, implied_prob, consensus, deviation,
            trade_cost, edge_net, is_signal, side_hint
        )
        """
    )
    con.sql(
        """
        create table mart_weather_edge as select * from (values
            ('NYC high > 82.5F','NYC',0.81,0.47,0.32,true)
        ) t(title, location, forecast_prob, kalshi_prob, edge_net, is_signal)
        """
    )
    return con


def test_load_alertable_filters_by_threshold_and_signal():
    con = _con_with_marts()
    alerts = load_alertable(con, min_edge=0.05)
    kinds = sorted(a.kind for a in alerts)
    # The 0.09 divergence and the 0.32 weather edge qualify; the 0.01 one is filtered.
    assert kinds == ["divergence", "weather"]
    assert alerts[0].edge_net == pytest.approx(0.32)  # sorted desc, weather first


def test_format_message_is_readable_and_bounded():
    con = _con_with_marts()
    msg = format_message(load_alertable(con, 0.05), 0.05)
    assert "EdgeRadar" in msg and "review only" in msg
    assert "NYC high" in msg
    assert len(msg) <= 1900


def test_format_message_empty():
    assert "no signals" in format_message([], 0.05)


def test_alert_refuses_when_execution_enabled(monkeypatch):
    monkeypatch.setenv("ENABLE_ORDER_EXECUTION", "true")
    from edgeradar.config import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="read-only"):
        run_alert(dry_run=True)
    get_settings.cache_clear()  # reset for other tests


def test_dashboard_module_compiles():
    path = Path(__file__).resolve().parents[1] / "src/edgeradar/dashboard/app.py"
    py_compile.compile(str(path), doraise=True)
