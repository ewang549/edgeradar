"""Phase 1 tests: adapters normalize correctly, dry-run is offline, ingestion is idempotent.

All tests run in --dry-run mode against the committed sample_responses fixtures, so
they need no network and spend no API quota.
"""

from __future__ import annotations

import socket

import pytest

from edgeradar.adapters.kalshi import KalshiAdapter
from edgeradar.adapters.manifold import ManifoldAdapter
from edgeradar.ingest import run_ingest
from edgeradar.storage import read_quotes


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Hard-guarantee the dry-run path makes no network calls: break sockets."""

    def _boom(*_a, **_k):  # pragma: no cover - only fires on a bug
        raise AssertionError("network access attempted during a dry-run test")

    monkeypatch.setattr(socket, "socket", _boom)


def test_far_future_dates_are_clamped():
    # Manifold/Polymarket sometimes use far-future "never closes" sentinels that
    # overflow pandas' datetime range; the helpers must clamp them to None.
    from edgeradar.adapters._util import ms_to_dt, parse_iso

    assert ms_to_dt(113_188_300_800_000) is None  # ~year 5555 in ms
    assert parse_iso("5555-12-31T23:59:00Z") is None
    assert parse_iso("2026-06-17T12:00:00Z") is not None  # normal dates still parse


def test_manifold_normalizes_binary_and_filters_rest():
    adapter = ManifoldAdapter(dry_run=True)
    quotes = adapter.run()
    # Sample has 4 active binary, 1 resolved, 1 multi-choice -> only 4 remain.
    assert len(quotes) == 4
    by_id = {q.market_id: q for q in quotes}
    assert by_id["9US900n98Z"].implied_prob == pytest.approx(0.2)
    assert all(q.source == "manifold" and q.outcome == "YES" for q in quotes)
    assert all(0.0 < q.implied_prob < 1.0 for q in quotes)


def test_kalshi_midpoint_and_filtering():
    adapter = KalshiAdapter(dry_run=True)
    quotes = adapter.run()
    # 2 active (kept), 1 settled (filtered), 1 illiquid active (kept, implied=None).
    assert len(quotes) == 3
    by_id = {q.market_id: q for q in quotes}
    # (0.45 + 0.48) / 2 = 0.465
    assert by_id["KXHIGHNY-26JUN17-B82.5"].implied_prob == pytest.approx(0.465)
    # (0.90 + 0.92) / 2 = 0.91
    assert by_id["KXNBAGAME-26JUN17BOSLAL-BOS"].implied_prob == pytest.approx(0.91)
    # Illiquid market: mid == 0 -> implied_prob is None (not fabricated).
    assert by_id["KXILLIQUID-26JUN17-EXAMPLE"].implied_prob is None


def test_dry_run_ingestion_lands_parquet(tmp_path, monkeypatch):
    # Point the lake at a temp dir so the test doesn't touch real data/.
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    from edgeradar.config import get_settings

    get_settings.cache_clear()  # settings are cached; re-read with the temp DATA_ROOT

    results = run_ingest("all", dry_run=True)
    assert {r.source for r in results} == {"manifold", "kalshi", "polymarket", "oddsapi"}
    assert all(r.clean_path is not None for r in results)

    df = read_quotes(data_root=str(tmp_path))
    assert len(df) == 14  # 4 manifold + 3 kalshi + 3 polymarket + 4 oddsapi


def test_reruns_are_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    from edgeradar.config import get_settings

    get_settings.cache_clear()

    run_ingest("all", dry_run=True)
    first = len(read_quotes(data_root=str(tmp_path)))
    run_ingest("all", dry_run=True)  # same fixed dry-run snapshot -> overwrites
    second = len(read_quotes(data_root=str(tmp_path)))
    assert first == second == 14  # no duplicates accumulate
