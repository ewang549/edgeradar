"""Tests for resolution diagnostics: counts, blocking, and plain-language reasons."""

from __future__ import annotations

import pandas as pd

from edgeradar.resolution_diagnostics import (
    compute_resolution_diagnostics,
    read_resolution_diagnostics,
    write_resolution_diagnostics,
)


def _markets(rows: list[dict]) -> pd.DataFrame:
    base = {"market_id": "m", "source": "s", "category": "other", "entities": frozenset()}
    return pd.DataFrame([{**base, **r} for r in rows])


def test_no_markets_yields_clear_reason():
    diag = compute_resolution_diagnostics(pd.DataFrame(), pd.DataFrame(), n_cross_platform=0)
    assert diag.n_markets == 0
    assert "no markets ingested yet" in diag.reasons[0]


def test_success_case_reports_event_count():
    markets = _markets(
        [
            {"market_id": "1", "source": "kalshi", "category": "sports"},
            {"market_id": "2", "source": "manifold", "category": "sports"},
        ]
    )
    pairs = pd.DataFrame([{"decision": "match", "confidence": 0.9, "category": "sports"}])
    diag = compute_resolution_diagnostics(markets, pairs, n_cross_platform=1)
    assert diag.n_cross_platform == 1
    assert "1 cross-platform event" in diag.reasons[0]


def test_no_overlapping_category_explains_why():
    # Kalshi only ever contributed "weather" markets; manifold only "crypto" —
    # no category has 2+ sources, so nothing could have been compared.
    markets = _markets(
        [
            {"market_id": "1", "source": "kalshi", "category": "weather"},
            {"market_id": "2", "source": "manifold", "category": "crypto"},
        ]
    )
    diag = compute_resolution_diagnostics(markets, pd.DataFrame(), n_cross_platform=0)
    assert diag.n_pairs_scored == 0
    joined = " ".join(diag.reasons)
    assert "kalshi" in joined and "weather" in joined
    assert "no other source covers" in joined


def test_pairs_scored_but_all_below_threshold_explains_why():
    markets = _markets(
        [
            {"market_id": "1", "source": "kalshi", "category": "sports"},
            {"market_id": "2", "source": "manifold", "category": "sports"},
        ]
    )
    pairs = pd.DataFrame(
        [
            {"decision": "no-match", "confidence": 0.42, "category": "sports"},
            {"decision": "no-match", "confidence": 0.55, "category": "sports"},
        ]
    )
    diag = compute_resolution_diagnostics(markets, pairs, n_cross_platform=0)
    assert diag.n_pairs_scored == 2
    assert diag.n_near_miss == 2
    assert diag.near_miss_max == 0.55
    joined = " ".join(diag.reasons)
    assert "2 pair(s) scored" in joined
    assert "0.55" in joined


def test_blocks_breakdown_counts_per_source_per_category():
    markets = _markets(
        [
            {
                "market_id": "1",
                "source": "kalshi",
                "category": "weather",
                "entities": frozenset({"chicago"}),
            },
            {"market_id": "2", "source": "kalshi", "category": "weather", "entities": frozenset()},
            {"market_id": "3", "source": "manifold", "category": "sports"},
        ]
    )
    diag = compute_resolution_diagnostics(markets, pd.DataFrame(), n_cross_platform=0)
    row = diag.blocks[(diag.blocks["source"] == "kalshi") & (diag.blocks["category"] == "weather")]
    assert int(row["n_markets"].iloc[0]) == 2
    assert int(row["n_with_entity"].iloc[0]) == 1


def test_write_and_read_roundtrip(tmp_path):
    markets = _markets([{"market_id": "1", "source": "kalshi", "category": "weather"}])
    diag = compute_resolution_diagnostics(markets, pd.DataFrame(), n_cross_platform=0)
    write_resolution_diagnostics(diag, data_root=str(tmp_path))
    summary, blocks = read_resolution_diagnostics(data_root=str(tmp_path))
    assert not summary.empty
    assert not blocks.empty
    assert summary.iloc[0]["n_markets"] == 1


def test_read_before_write_returns_empty_frames(tmp_path):
    summary, blocks = read_resolution_diagnostics(data_root=str(tmp_path))
    assert summary.empty
    assert blocks.empty
