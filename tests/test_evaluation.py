"""Phase 6 tests: signal scoring math (hit, PnL net of fees, calibration)."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from edgeradar.evaluation import _pnl_net, _resolve_one, log_signals, score_signals


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


# --------------------------------------------------------------------------- #
# Task 6: log_signals (reads mart_divergence/mart_weather_edge), auto-resolve
# parsing, and a simulated multi-day signal_log -> score accumulation flow.
# --------------------------------------------------------------------------- #


def _write_divergence_mart(db_path: str, rows: list[dict]) -> None:
    """Create a minimal mart_divergence table for log_signals() to read."""
    import duckdb

    df = pd.DataFrame(rows)  # noqa: F841 - duckdb.sql() looks this up by local variable name
    con = duckdb.connect(db_path)
    con.sql("create table mart_divergence as select * from df")
    con.close()


def test_log_signals_reads_mart_divergence_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    from edgeradar.config import get_settings

    get_settings.cache_clear()
    db_path = str(tmp_path / "wh.duckdb")
    _write_divergence_mart(
        db_path,
        [
            {
                "event_id": "e1",
                "market_id": "MKT1",
                "source": "kalshi",
                "title": "test market",
                "is_signal": True,
                "deviation": -0.10,  # < 0 -> implied YES side underpriced
                "consensus": 0.60,
                "implied_prob": 0.50,
                "edge_net": 0.08,
                "trade_cost": 0.02,
                "snapshot_ts": datetime(2026, 6, 17, tzinfo=timezone.utc),
            }
        ],
    )
    log = log_signals(data_root=str(tmp_path), duckdb_path=db_path)
    assert len(log) == 1
    assert log.iloc[0]["side"] == "YES"
    assert log.iloc[0]["predicted_prob_side"] == pytest.approx(0.60)
    assert bool(log.iloc[0]["tradeable"]) is True

    # Re-running against the SAME mart state must not double-count (idempotent
    # on signal_key, derived from signal_type|market_id|signal_ts|side).
    again = log_signals(data_root=str(tmp_path), duckdb_path=db_path)
    assert len(again) == 1


def test_multi_day_signal_accumulation_and_scoring(tmp_path, monkeypatch):
    # Simulates the real forward-evaluation loop: day 1 logs one signal, day 2's
    # ingest produces a second (new) signal alongside the first; signal_log
    # accumulates both without duplicating day 1's. Both then resolve and score.
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    from edgeradar.config import get_settings

    get_settings.cache_clear()

    day1_db = str(tmp_path / "day1.duckdb")
    _write_divergence_mart(
        day1_db,
        [
            {
                "event_id": "e1",
                "market_id": "MKT1",
                "source": "kalshi",
                "title": "day-1 market",
                "is_signal": True,
                "deviation": -0.10,
                "consensus": 0.60,
                "implied_prob": 0.50,
                "edge_net": 0.08,
                "trade_cost": 0.02,
                "snapshot_ts": datetime(2026, 6, 17, tzinfo=timezone.utc),
            }
        ],
    )
    log_day1 = log_signals(data_root=str(tmp_path), duckdb_path=day1_db)
    assert len(log_day1) == 1

    day2_db = str(tmp_path / "day2.duckdb")
    _write_divergence_mart(
        day2_db,
        [
            # Same signal as day 1 (must not duplicate)...
            {
                "event_id": "e1",
                "market_id": "MKT1",
                "source": "kalshi",
                "title": "day-1 market",
                "is_signal": True,
                "deviation": -0.10,
                "consensus": 0.60,
                "implied_prob": 0.50,
                "edge_net": 0.08,
                "trade_cost": 0.02,
                "snapshot_ts": datetime(2026, 6, 17, tzinfo=timezone.utc),
            },
            # ...plus a genuinely new one that only appeared on day 2.
            {
                "event_id": "e2",
                "market_id": "MKT2",
                "source": "kalshi",
                "title": "day-2 market",
                "is_signal": True,
                "deviation": 0.15,  # > 0 -> NO side
                "consensus": 0.30,
                "implied_prob": 0.45,
                "edge_net": 0.10,
                "trade_cost": 0.01,
                "snapshot_ts": datetime(2026, 6, 18, tzinfo=timezone.utc),
            },
        ],
    )
    log_day2 = log_signals(data_root=str(tmp_path), duckdb_path=day2_db)
    assert len(log_day2) == 2  # accumulated, not duplicated

    resolutions = tmp_path / "res.csv"
    resolutions.write_text("market_id,outcome\nMKT1,1\nMKT2,0\n")
    scored, summary = score_signals(data_root=str(tmp_path), resolutions_path=str(resolutions))
    assert summary.n_resolved == 2
    # MKT1: YES side, outcome YES -> hit. MKT2: NO side (deviation>0), outcome NO -> hit.
    assert summary.hit_rate == 1.0
    assert summary.pnl_net_total is not None
    assert summary.calibration  # calibration buckets were produced


def test_resolve_one_parses_kalshi_and_manifold_settlement(monkeypatch):
    import httpx

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get(url, timeout=None):
        if "kalshi" in url:
            return _Resp(200, {"market": {"result": "yes", "status": "finalized"}})
        return _Resp(200, {"isResolved": True, "resolution": "NO"})

    monkeypatch.setattr(httpx, "get", fake_get)
    outcome, detail = _resolve_one("kalshi", "ANY")
    assert outcome == 1
    assert "settled yes" in detail

    outcome, detail = _resolve_one("manifold", "ANY")
    assert outcome == 0
    assert "resolved NO" in detail


def test_resolve_one_handles_unresolved_and_errors(monkeypatch):
    import httpx

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def fake_get_unresolved(url, timeout=None):
        if "kalshi" in url:
            return _Resp(200, {"market": {"result": "", "status": "active"}})
        return _Resp(200, {"isResolved": False, "resolution": None})

    monkeypatch.setattr(httpx, "get", fake_get_unresolved)
    outcome, detail = _resolve_one("kalshi", "ANY")
    assert outcome is None
    assert "not settled" in detail
    outcome, detail = _resolve_one("manifold", "ANY")
    assert outcome is None
    assert "not resolved" in detail

    # Fail-soft on a network/HTTP error -- never raises, just reports "unresolved".
    def fake_get_error(url, timeout=None):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "get", fake_get_error)
    outcome, detail = _resolve_one("kalshi", "ANY")
    assert outcome is None
    assert "error" in detail
