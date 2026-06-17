"""Phase 4 tests: entity resolution groups same-event markets correctly.

Runs against the dry-run fixtures, which include two deliberately matchable
cross-platform pairs (NBA game + NYC temperature on Manifold and Kalshi).
"""

from __future__ import annotations

import pytest

from edgeradar.entity_resolution import (
    guess_category,
    resolve,
    title_similarity,
    tokenize,
)
from edgeradar.ingest import run_ingest


@pytest.fixture()
def landed(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    from edgeradar.config import get_settings

    get_settings.cache_clear()
    run_ingest("all", dry_run=True)
    return str(tmp_path)


def test_feature_extraction():
    assert guess_category("Will the high temperature in NYC be above 82.5F?") == "weather"
    assert guess_category("Will the Boston Celtics beat the Los Angeles Lakers?") == "sports"
    assert "celtics" in tokenize("Will the Boston Celtics win?")
    assert "will" not in tokenize("Will it rain?")  # stopword dropped


def test_similar_titles_score_high_distinct_low():
    a = tokenize("Will the Boston Celtics beat the Los Angeles Lakers on June 17?")
    b = tokenize("Will the Boston Celtics beat the Los Angeles Lakers on Jun 17?")
    c = tokenize("Will August 2026 US retail sales rise 0.5%?")
    assert title_similarity(a, b) > 0.7
    assert title_similarity(a, c) < 0.3


def test_known_cross_platform_pairs_are_grouped(landed):
    res = resolve(data_root=landed, write=False)
    em = res.event_map

    # The NBA markets on both platforms should share one event_id.
    nba = em[em["market_id"].isin(["ManifoldNBA01", "KXNBAGAME-26JUN17BOSLAL-BOS"])]
    assert nba["event_id"].nunique() == 1

    # The NYC temperature markets should share one event_id.
    wx = em[em["market_id"].isin(["ManifoldWX01", "KXHIGHNY-26JUN17-B82.5"])]
    assert wx["event_id"].nunique() == 1

    # Exactly two cross-platform events; unrelated markets stay singletons.
    assert res.n_cross_platform == 2
    assert nba["event_id"].iloc[0] != wx["event_id"].iloc[0]


def test_block_override_separates_a_pair(tmp_path, monkeypatch):
    # Isolated two-market lake so no third source can transitively bridge them.
    monkeypatch.setenv("DATA_ROOT", str(tmp_path))
    from datetime import datetime, timezone
    from decimal import Decimal

    from edgeradar.config import get_settings
    from edgeradar.models import MarketQuote
    from edgeradar.storage import write_quotes_grouped

    get_settings.cache_clear()
    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    quotes = [
        MarketQuote(
            source="manifold",
            market_id="NBA_A",
            outcome="YES",
            title="Will the Boston Celtics beat the Los Angeles Lakers on June 17?",
            price=Decimal("0.88"),
            implied_prob=0.88,
            fee_adj_prob=0.88,
            snapshot_ts=ts,
        ),
        MarketQuote(
            source="kalshi",
            market_id="NBA_B",
            outcome="YES",
            title="Will the Boston Celtics beat the Los Angeles Lakers on Jun 17?",
            price=Decimal("0.91"),
            implied_prob=0.91,
            fee_adj_prob=0.91,
            snapshot_ts=ts,
        ),
    ]
    write_quotes_grouped(quotes, data_root=str(tmp_path))

    # Without an override they group; with a block they don't.
    grouped = resolve(data_root=str(tmp_path), write=False).event_map
    assert grouped[grouped["market_id"].isin(["NBA_A", "NBA_B"])]["event_id"].nunique() == 1

    override = tmp_path / "ov.csv"
    override.write_text(
        "source_a,market_id_a,source_b,market_id_b,relation\nmanifold,NBA_A,kalshi,NBA_B,block\n"
    )
    res = resolve(data_root=str(tmp_path), overrides_path=str(override), write=False)
    nba = res.event_map[res.event_map["market_id"].isin(["NBA_A", "NBA_B"])]
    assert nba["event_id"].nunique() == 2  # blocked -> not grouped
    assert res.overrides_applied >= 1


def test_confidence_in_unit_interval(landed):
    res = resolve(data_root=landed, write=False)
    conf = res.event_map["match_confidence"].dropna()
    assert ((conf >= 0) & (conf <= 1)).all()


def test_different_temperature_thresholds_not_merged(tmp_path, monkeypatch):
    # Regression: near-identical titles that differ only in the threshold number
    # (e.g. a ladder of Houston temperature buckets) must NOT collapse into one event.
    from datetime import datetime, timezone
    from decimal import Decimal

    from edgeradar.models import MarketQuote
    from edgeradar.storage import write_quotes_grouped

    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    quotes = [
        MarketQuote(
            source="kalshi",
            market_id="H96",
            outcome="YES",
            title="Will the highest temperature in Houston be 96F or higher on June 17?",
            price=Decimal("0.46"),
            implied_prob=0.46,
            fee_adj_prob=0.46,
            snapshot_ts=ts,
        ),
        MarketQuote(
            source="polymarket",
            market_id="H97",
            outcome="YES",
            title="Will the highest temperature in Houston be 97F or higher on June 17?",
            price=Decimal("0.30"),
            implied_prob=0.30,
            fee_adj_prob=0.30,
            snapshot_ts=ts,
        ),
    ]
    write_quotes_grouped(quotes, data_root=str(tmp_path))
    res = resolve(data_root=str(tmp_path), write=False)
    ev = res.event_map.set_index("market_id")["event_id"]
    assert ev["H96"] != ev["H97"]  # different thresholds -> different events
    assert res.n_cross_platform == 0
