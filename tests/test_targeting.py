"""Tests for category/keyword ingestion targeting (Task 2)."""

from __future__ import annotations

from edgeradar.targeting import KNOWN_CATEGORIES, resolve_categories


def test_resolve_known_categories_returns_concrete_filters():
    t = resolve_categories(["world_cup", "crypto"])
    assert "KXMENWORLDCUP" in t.kalshi_series
    assert "KXBTCD" in t.kalshi_series or "KXBTC" in t.kalshi_series
    assert any("world cup" in k.lower() for k in t.keywords)
    assert any("bitcoin" in k.lower() for k in t.keywords)


def test_resolve_unknown_category_is_inert_not_an_error():
    t = resolve_categories(["not_a_real_category"])
    assert t.kalshi_series == []
    assert t.keywords == []
    assert t.oddsapi_sports == []


def test_resolve_no_categories_returns_empty():
    t = resolve_categories(None)
    assert t.kalshi_series == t.keywords == t.oddsapi_sports == []
    t2 = resolve_categories([])
    assert t2.kalshi_series == t2.keywords == t2.oddsapi_sports == []


def test_resolve_deduplicates_and_is_case_insensitive():
    t = resolve_categories(["crypto", "CRYPTO", "Crypto"])
    assert len(t.kalshi_series) == len(set(t.kalshi_series))


def test_known_categories_is_nonempty_and_sorted():
    assert sorted(KNOWN_CATEGORIES) == KNOWN_CATEGORIES
    assert "weather" in KNOWN_CATEGORIES
    assert "world_cup" in KNOWN_CATEGORIES


def test_ingest_wires_categories_into_kalshi_adapter(monkeypatch):
    # run_ingest("kalshi", categories=[...]) should construct a KalshiAdapter with
    # the resolved series_tickers, not the default "pull everything" adapter.
    import edgeradar.ingest as ingest_mod

    captured = {}

    class _FakeAdapter:
        def __init__(self, *, dry_run=False, series_tickers=None):
            captured["series_tickers"] = series_tickers

        def fetch(self):
            return []

        def normalize(self, raw):
            return []

    monkeypatch.setitem(ingest_mod.REGISTRY, "kalshi", _FakeAdapter)
    monkeypatch.setattr(ingest_mod, "KalshiAdapter", _FakeAdapter)
    ingest_mod.run_ingest("kalshi", dry_run=True, categories=["world_cup"])
    assert captured["series_tickers"]
    assert "KXMENWORLDCUP" in captured["series_tickers"]
