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
    # 2 liquid two-sided quotes + 1 stale-price fallback kept; settled, fully-dead
    # illiquid, and the MVE combo basket are all dropped.
    assert len(quotes) == 3
    by_id = {q.market_id: q for q in quotes}
    # (0.45 + 0.48) / 2 = 0.465
    assert by_id["KXHIGHNY-26JUN17-B82.5"].implied_prob == pytest.approx(0.465)
    assert by_id["KXHIGHNY-26JUN17-B82.5"].price_is_stale is False
    # (0.90 + 0.92) / 2 = 0.91
    assert by_id["KXNBAGAME-26JUN17BOSLAL-BOS"].implied_prob == pytest.approx(0.91)
    # Fully dead market (no quotes, no volume, no last price) -> dropped entirely.
    assert "KXILLIQUID-26JUN17-EXAMPLE" not in by_id
    # MVE combo basket -> dropped entirely, regardless of its (also-illiquid) quotes.
    assert "KXMVESPORTSMULTIGAMEEXTENDED-S2026EXAMPLE-COMBO" not in by_id
    # No live bid/ask but real volume + a recent trade -> kept, flagged as stale.
    stale = by_id["KXSTALEFALLBACK-26JUN17-EXAMPLE"]
    assert stale.implied_prob == pytest.approx(0.33)
    assert stale.price_is_stale is True


def test_kalshi_combo_market_helpers():
    from edgeradar.adapters.kalshi import has_liquidity_signal, is_combo_market

    combo = {
        "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-X",
        "mve_collection_ticker": "KXMVESPORTSMULTIGAMEEXTENDED-R",
        "mve_selected_legs": [{"market_ticker": "FOO-BAR", "side": "yes"}],
        "market_type": "binary",  # combos report "binary" too -- not a reliable signal
    }
    normal = {"ticker": "KXHIGHNY-26JUN17-B82.5", "market_type": "binary"}
    assert is_combo_market(combo) is True
    assert is_combo_market(normal) is False
    # ticker-prefix-only detection (no mve_* fields present) still catches it.
    assert is_combo_market({"ticker": "KXMVECROSSCATEGORY-Z"}) is True

    assert has_liquidity_signal({"liquidity_dollars": "5.00"}, min_dollars=1.0) is True
    assert has_liquidity_signal({"volume_24h_fp": "0.00"}, min_dollars=1.0) is False


def _fake_kalshi_market(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "market_type": "binary",
        "status": "active",
        "title": f"Fake market {ticker}",
        "yes_bid_dollars": "0.40",
        "yes_ask_dollars": "0.42",
        "last_price_dollars": "0.41",
        "liquidity_dollars": "100.0",
    }


def test_kalshi_pagination_follows_cursor_and_respects_max_pages(monkeypatch):
    calls: list[dict] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(dict(params))
        page_cursor = params.get("cursor") or "p0"
        next_cursor = {"p0": "p1", "p1": "p2"}.get(page_cursor, "")

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {
                    "cursor": next_cursor,
                    "markets": [_fake_kalshi_market(f"KXFAKE-{page_cursor}")],
                }

        return _Resp()

    monkeypatch.setattr("edgeradar.adapters.kalshi.httpx.get", fake_get)
    adapter = KalshiAdapter(dry_run=False, max_pages=2)
    quotes = adapter.run()
    # max_pages=2 stops after 2 calls even though the server offered a 3rd cursor.
    assert len(calls) == 2
    assert len(quotes) == 2


def test_kalshi_series_ticker_targeting(monkeypatch):
    calls: list[dict] = []

    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append(dict(params))
        series = params["series_ticker"]

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"cursor": "", "markets": [_fake_kalshi_market(f"KX-{series}-X")]}

        return _Resp()

    monkeypatch.setattr("edgeradar.adapters.kalshi.httpx.get", fake_get)
    adapter = KalshiAdapter(dry_run=False, series_tickers=["KXMENWORLDCUP", "KXBTCD"])
    quotes = adapter.run()
    assert {c["series_ticker"] for c in calls} == {"KXMENWORLDCUP", "KXBTCD"}
    assert len(quotes) == 2


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
