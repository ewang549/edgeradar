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


def test_block_override_separates_a_pair(landed, tmp_path, monkeypatch):
    # Force the NBA pair apart via a manual block override.
    override = tmp_path / "ov.csv"
    override.write_text(
        "source_a,market_id_a,source_b,market_id_b,relation\n"
        "manifold,ManifoldNBA01,kalshi,KXNBAGAME-26JUN17BOSLAL-BOS,block\n"
    )
    res = resolve(data_root=landed, overrides_path=str(override), write=False)
    em = res.event_map
    nba = em[em["market_id"].isin(["ManifoldNBA01", "KXNBAGAME-26JUN17BOSLAL-BOS"])]
    assert nba["event_id"].nunique() == 2  # blocked -> not grouped
    assert res.overrides_applied >= 1


def test_confidence_in_unit_interval(landed):
    res = resolve(data_root=landed, write=False)
    conf = res.event_map["match_confidence"].dropna()
    assert ((conf >= 0) & (conf <= 1)).all()
