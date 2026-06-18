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


def test_auto_resolutions_are_merged_and_scored(tmp_path, monkeypatch):
    # An auto-resolved outcome (data/marts/resolutions_auto.csv) should be picked up
    # by scoring with no manual seed file involved.
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    from edgeradar.config import get_settings

    get_settings.cache_clear()
    _write_log(
        tmp_path,
        [
            {
                "signal_key": "k1",
                "signal_type": "divergence",
                "event_id": "e1",
                "market_id": "AUTO1",
                "source": "kalshi",
                "side": "YES",
                "platform_prob": 0.40,
                "reference_prob": 0.80,
                "predicted_prob_side": 0.80,
                "edge_net": 0.30,
                "trade_cost": 0.02,
                "tradeable": True,
                "signal_ts": "2026-06-17",
                "title": "auto market",
            }
        ],
    )
    # Simulate what auto_resolve would have written (no network in the test).
    marts = tmp_path / "marts"
    marts.mkdir(parents=True, exist_ok=True)
    (marts / "resolutions_auto.csv").write_text("market_id,outcome,source\nAUTO1,1,kalshi\n")

    _, summary = score_signals(
        data_root=str(tmp_path), resolutions_path=str(tmp_path / "nonexistent_seed.csv")
    )
    assert summary.n_resolved == 1  # picked up purely from the auto file
    assert summary.hit_rate == 1.0


def test_backfill_scores_settled_markets(tmp_path, monkeypatch):
    # Backfill should score the 3 liquid settled markets in the fixture (the 0-volume
    # one is excluded) and write outcomes into resolutions_auto.csv.
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    from edgeradar.config import get_settings
    from edgeradar.evaluation import backfill_kalshi_calibration

    get_settings.cache_clear()
    s = backfill_kalshi_calibration(dry_run=True, data_root=str(tmp_path))
    assert s.n_markets == 3
    assert 0.0 <= s.accuracy <= 1.0
    assert 0.0 <= s.brier <= 1.0
    # per market-type breakdown is present (weather + sports in the fixture)
    groups = {g["group"] for g in s.by_group}
    assert {"weather", "sports"} <= groups
    # outcomes were written for auto-resolution
    auto = pd.read_csv(tmp_path / "marts" / "resolutions_auto.csv")
    assert "KXNBAGAME-26JUN17BOSLAL-BOS" in set(auto["market_id"])


def test_market_group_classification():
    from edgeradar.evaluation import market_group

    assert market_group("KXBTC15M-26JUN181115-15") == "crypto"
    assert market_group("KXNBAGAME-26JUN17BOSLAL-BOS") == "sports"
    assert market_group("KXHIGHNY-26JUN17-B82.5") == "weather"
    assert market_group("KXCPIYOY-26") == "other"


def test_auto_resolve_dry_run_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    from edgeradar.config import get_settings
    from edgeradar.evaluation import auto_resolve

    get_settings.cache_clear()
    assert auto_resolve(data_root=str(tmp_path), dry_run=True) == (0, 0)


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
