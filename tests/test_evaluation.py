"""Phase 6 tests: signal scoring math (hit, PnL net of fees, calibration)."""

from __future__ import annotations

import pandas as pd
import pytest

from edgeradar.evaluation import _pnl_net, score_signals


def test_pnl_net_yes_win_and_loss():
    # Buy YES at 0.40, no cost. Wins -> +0.60 ; loses -> -0.40.
    assert _pnl_net("YES", 0.40, 1, 0.0) == pytest.approx(0.60)
    assert _pnl_net("YES", 0.40, 0, 0.0) == pytest.approx(-0.40)


def test_pnl_net_no_side_and_cost():
    # Buy NO at price (1-0.40)=0.60. Outcome NO(0) -> +0.40 ; minus 0.05 cost.
    assert _pnl_net("NO", 0.40, 0, 0.05) == pytest.approx(0.40 - 0.05)
    # Outcome YES(1) -> lose 0.60.
    assert _pnl_net("NO", 0.40, 1, 0.0) == pytest.approx(-0.60)


def _write_log(tmp_path, rows):
    d = tmp_path / "marts"
    d.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(d / "signal_log.parquet", index=False)


def test_score_signals_hit_and_pnl(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    from edgeradar.config import get_settings

    get_settings.cache_clear()

    _write_log(
        tmp_path,
        [
            {
                "signal_key": "k1",
                "signal_type": "weather",
                "event_id": None,
                "market_id": "MKT1",
                "source": "kalshi",
                "side": "YES",
                "platform_prob": 0.40,
                "reference_prob": 0.80,
                "predicted_prob_side": 0.80,
                "edge_net": 0.30,
                "trade_cost": 0.02,
                "tradeable": True,
                "signal_ts": "2026-06-17",
                "title": "test market",
            }
        ],
    )
    resolutions = tmp_path / "res.csv"
    resolutions.write_text("market_id,outcome\nMKT1,1\n")

    scored, summary = score_signals(data_root=str(tmp_path), resolutions_path=str(resolutions))
    assert summary.n_resolved == 1
    assert summary.hit_rate == 1.0  # YES side, outcome YES -> hit
    # PnL: YES at 0.40 wins -> 0.60, minus 0.02 cost = 0.58.
    assert summary.pnl_net_total == pytest.approx(0.58)
    assert scored.iloc[0]["hit"] == 1


def test_score_signals_no_resolutions_is_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    from edgeradar.config import get_settings

    get_settings.cache_clear()
    _write_log(tmp_path, [])  # empty log
    scored, summary = score_signals(
        data_root=str(tmp_path), resolutions_path=str(tmp_path / "none.csv")
    )
    assert summary.n_resolved == 0
    assert summary.hit_rate is None
