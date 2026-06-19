"""Phase 4 tests: entity resolution groups same-event markets correctly.

Runs against the dry-run fixtures, which include two deliberately matchable
cross-platform pairs (NBA game + NYC temperature on Manifold and Kalshi).
"""

from __future__ import annotations

import pytest

from edgeradar.entity_resolution import (
    extract_entities,
    guess_category,
    resolve,
    subject_tokens,
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


def test_opposite_game_sides_not_merged(tmp_path, monkeypatch):
    # Regression: "Celtics win" and "Lakers win" are complementary, not the same
    # event — they must not merge (their probabilities would be averaged otherwise).
    from datetime import datetime, timezone
    from decimal import Decimal

    from edgeradar.models import MarketQuote
    from edgeradar.storage import write_quotes_grouped

    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    quotes = [
        MarketQuote(
            source="kalshi",
            market_id="CELT",
            outcome="YES",
            title="Will the Boston Celtics beat the Los Angeles Lakers?",
            price=Decimal("0.88"),
            implied_prob=0.88,
            fee_adj_prob=0.88,
            snapshot_ts=ts,
        ),
        MarketQuote(
            source="oddsapi",
            market_id="CELT2",
            outcome="YES",
            title="Boston Celtics to win vs Los Angeles Lakers (NBA)",
            price=Decimal("0.86"),
            implied_prob=0.86,
            fee_adj_prob=0.86,
            snapshot_ts=ts,
        ),
        MarketQuote(
            source="oddsapi",
            market_id="LAL",
            outcome="YES",
            title="Los Angeles Lakers to win vs Boston Celtics (NBA)",
            price=Decimal("0.14"),
            implied_prob=0.14,
            fee_adj_prob=0.14,
            snapshot_ts=ts,
        ),
    ]
    write_quotes_grouped(quotes, data_root=str(tmp_path))
    ev = resolve(data_root=str(tmp_path), write=False).event_map.set_index("market_id")["event_id"]
    # Both "Celtics win" markets group together...
    assert ev["CELT"] == ev["CELT2"]
    # ...but the "Lakers win" market is a separate event.
    assert ev["LAL"] != ev["CELT"]


# --------------------------------------------------------------------------- #
# Task 3: alias normalization + entity sub-blocking
# --------------------------------------------------------------------------- #


def test_alias_normalization_collapses_country_synonyms():
    # "USA" and "United States" must tokenize to the same canonical entity so
    # title_similarity treats them as identical, not merely similar.
    assert tokenize("Will the USA win the 2026 World Cup?") == tokenize(
        "Will the United States win the 2026 World Cup?"
    )
    assert "united_states" in tokenize("Will USA win?")
    assert "bitcoin" in tokenize("Will BTC hit $100k?")


def test_extract_entities_recognizes_countries_cities_and_tickers():
    assert extract_entities("Will Brazil win the 2026 World Cup?") == frozenset({"brazil"})
    assert extract_entities("Will the USA win the 2026 World Cup?") == frozenset({"united_states"})
    assert extract_entities("Will BTC hit $100k?") == frozenset({"bitcoin"})
    # A team/ticker our small gazetteer doesn't know about -> no entity (a
    # documented blocking trade-off, not a crash).
    assert extract_entities("Will the Zorbinaut Quasars win the cup?") == frozenset()


def test_subject_tokens_generalizes_beyond_win_words():
    # The old implementation only fired on "beat"/"win"-style words; it must also
    # separate "be"-templated subjects (candidates, countries) from their predicate.
    assert subject_tokens("Will Dwayne 'The Rock' Johnson be the 2028 nominee?") == frozenset(
        {"dwayne", "rock", "johnson"}
    )
    assert subject_tokens("Will Türkiye finish last in Group D?") == frozenset({"türkiye"})
    # "of" is captured as part of the proper-noun span but dropped as a stopword.
    assert subject_tokens("Will the Democratic Republic of Congo win Group K?") == frozenset(
        {"democratic", "republic", "congo"}
    )
    # No capitalized subject (e.g. weather) -> no constraint.
    assert subject_tokens("Will the high temperature be above 90F?") is None


def test_different_countries_with_templated_titles_stay_separate(tmp_path, monkeypatch):
    # Regression for a real bug found on live World Cup data: near-identical
    # boilerplate titles for DIFFERENT countries (sharing every token except the
    # country name) must never merge, even transitively through a third, fully
    # generic "bridge" market that has neither a recognized entity nor a subject.
    from datetime import datetime, timezone
    from decimal import Decimal

    from edgeradar.models import MarketQuote
    from edgeradar.storage import write_quotes_grouped

    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    quotes = [
        MarketQuote(
            source="kalshi",
            market_id="BOS",
            outcome="YES",
            title="Will Bosnia and Herzegovina be the highest-scoring team in Group F?",
            price=Decimal("0.10"),
            implied_prob=0.10,
            fee_adj_prob=0.10,
            snapshot_ts=ts,
        ),
        MarketQuote(
            source="polymarket",
            market_id="ARG",
            outcome="YES",
            title="Will Argentina be the highest-scoring team in Group J?",
            price=Decimal("0.30"),
            implied_prob=0.30,
            fee_adj_prob=0.30,
            snapshot_ts=ts,
        ),
        MarketQuote(
            source="manifold",
            market_id="BRIDGE",
            outcome="YES",
            title="Will every team score a goal at the 2026 FIFA World Cup?",
            price=Decimal("0.50"),
            implied_prob=0.50,
            fee_adj_prob=0.50,
            snapshot_ts=ts,
        ),
    ]
    write_quotes_grouped(quotes, data_root=str(tmp_path))
    ev = resolve(data_root=str(tmp_path), write=False).event_map.set_index("market_id")["event_id"]
    assert ev["BOS"] != ev["ARG"]
    assert ev["BOS"] != ev["BRIDGE"]
    assert ev["ARG"] != ev["BRIDGE"]


def test_same_country_different_platforms_still_merge(tmp_path, monkeypatch):
    # The entity guard must not be so strict that it blocks the legitimate case:
    # the SAME country, worded differently ("USA" vs "United States") across
    # platforms, for the SAME underlying question.
    from datetime import datetime, timezone
    from decimal import Decimal

    from edgeradar.models import MarketQuote
    from edgeradar.storage import write_quotes_grouped

    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    quotes = [
        MarketQuote(
            source="kalshi",
            market_id="USA1",
            outcome="YES",
            title="Will the USA win the 2026 Men's World Cup?",
            price=Decimal("0.12"),
            implied_prob=0.12,
            fee_adj_prob=0.12,
            snapshot_ts=ts,
        ),
        MarketQuote(
            source="manifold",
            market_id="USA2",
            outcome="YES",
            title="Will the United States win the 2026 FIFA World Cup?",
            price=Decimal("0.13"),
            implied_prob=0.13,
            fee_adj_prob=0.13,
            snapshot_ts=ts,
        ),
    ]
    write_quotes_grouped(quotes, data_root=str(tmp_path))
    ev = resolve(data_root=str(tmp_path), write=False).event_map.set_index("market_id")["event_id"]
    assert ev["USA1"] == ev["USA2"]


def test_manual_override_works_across_entity_subblocks(tmp_path, monkeypatch):
    # A human override must win even for a pair sub-blocking would never compare
    # (different/no recognized entities) — overrides aren't subject to the
    # automatic blocking trade-off.
    from datetime import datetime, timezone
    from decimal import Decimal

    from edgeradar.models import MarketQuote
    from edgeradar.storage import write_quotes_grouped

    ts = datetime(2025, 1, 1, tzinfo=timezone.utc)
    quotes = [
        MarketQuote(
            source="kalshi",
            market_id="ZORB",
            outcome="YES",
            title="Will the Zorbinaut Quasars win the championship?",
            price=Decimal("0.40"),
            implied_prob=0.40,
            fee_adj_prob=0.40,
            snapshot_ts=ts,
        ),
        MarketQuote(
            source="manifold",
            market_id="QUAS",
            outcome="YES",
            title="Quasars to win the title this season",
            price=Decimal("0.42"),
            implied_prob=0.42,
            fee_adj_prob=0.42,
            snapshot_ts=ts,
        ),
    ]
    write_quotes_grouped(quotes, data_root=str(tmp_path))
    override = tmp_path / "ov.csv"
    override.write_text(
        "source_a,market_id_a,source_b,market_id_b,relation\nkalshi,ZORB,manifold,QUAS,match\n"
    )
    res = resolve(data_root=str(tmp_path), overrides_path=str(override), write=False)
    ev = res.event_map.set_index("market_id")["event_id"]
    assert ev["ZORB"] == ev["QUAS"]
    assert res.overrides_applied >= 1


def test_embedding_layer_is_fail_soft_without_optional_dependency(landed, capsys):
    # sentence-transformers is NOT a dev/CI dependency by design (see pyproject's
    # `embeddings` extra) — `use_embeddings=True` must degrade to a no-op, not crash.
    from edgeradar.entity_resolution import embedding_candidate_pairs

    assert embedding_candidate_pairs([{"source": "a", "market_id": "1", "title": "x"}]) == []
    out = capsys.readouterr().out
    assert "sentence-transformers not installed" in out

    res = resolve(data_root=landed, write=False, use_embeddings=True)
    # Falls back to exactly the std-lib-only result: same cross-platform count.
    assert res.n_cross_platform == 2
