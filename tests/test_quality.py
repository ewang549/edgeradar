"""Tests for the data-quality / observability module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from edgeradar.quality import (
    assess_source,
    combo_exclusion_rate,
    compute_quality_report,
    duplicate_rate,
    null_rate,
    prob_bounds_violations,
    read_quality_report,
    stale_price_rate,
    write_quality_report,
)

KEY = ["source", "market_id", "outcome", "snapshot_ts"]


def _quote_frame(snap: datetime, n: int = 3, source: str = "kalshi") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "source": [source] * n,
            "market_id": [f"m{i}" for i in range(n)],
            "outcome": ["YES"] * n,
            "title": [f"Market {i}" for i in range(n)],
            "price": [0.5] * n,
            "implied_prob": [0.5] * n,
            "snapshot_ts": [snap] * n,
        }
    )


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_null_rate():
    assert null_rate(pd.Series([1.0, None, None, 1.0])) == 0.5
    assert null_rate(pd.Series([], dtype=float)) == 0.0


def test_duplicate_rate_detects_repeated_natural_key():
    snap = datetime(2026, 6, 18, tzinfo=timezone.utc)
    df = _quote_frame(snap, n=2)
    clean = duplicate_rate(df, KEY)
    assert clean == 0.0
    dup = pd.concat([df, df.iloc[[0]]], ignore_index=True)
    assert duplicate_rate(dup, KEY) > 0.0


def test_prob_bounds_violations():
    assert prob_bounds_violations(pd.Series([0.5, 1.2, -0.1, 0.9])) == 2
    assert prob_bounds_violations(pd.Series([0.0, 1.0, 0.5])) == 0


def test_stale_price_rate():
    assert stale_price_rate(pd.Series([True, False, False, False])) == 0.25
    assert stale_price_rate(pd.Series([], dtype=bool)) == 0.0
    assert stale_price_rate(None) == 0.0


def test_combo_exclusion_rate_detects_mve_baskets():
    payloads = pd.Series(
        [
            {"ticker": "KXHIGHNY-26JUN17-B82.5"},  # normal market
            {"ticker": "KXMVESPORTSMULTIGAMEEXTENDED-X", "mve_collection_ticker": "X-R"},
            {"ticker": "KXMVECROSSCATEGORY-Y"},
        ]
    )
    assert combo_exclusion_rate(payloads) == pytest.approx(2 / 3)
    # 0.0 for a source with no such concept (e.g. Manifold payloads).
    assert combo_exclusion_rate(pd.Series([{"id": "abc"}, {"id": "def"}])) == 0.0
    assert combo_exclusion_rate(pd.Series([], dtype=object)) == 0.0
    # JSON-string payloads (the raw zone's actual on-disk format) work too.
    import json

    json_payloads = pd.Series([json.dumps({"ticker": "KXMVESPORTSMULTIGAMEEXTENDED-X"})])
    assert combo_exclusion_rate(json_payloads) == 1.0


# --------------------------------------------------------------------------- #
# assess_source
# --------------------------------------------------------------------------- #


def test_fresh_clean_source_is_healthy():
    now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    df = _quote_frame(now - timedelta(minutes=2), n=5)
    h = assess_source(df, "kalshi", now=now)
    assert h.n_quotes == 5
    assert h.duplicate_rate == 0.0
    assert h.prob_violations == 0
    assert h.issues == ["healthy"]
    assert h.reliability_grade in {"A", "B"}


def test_stale_and_duplicated_source_flags_issues():
    now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(hours=10)
    df = _quote_frame(old, n=3)
    df = pd.concat([df, df.iloc[[0]]], ignore_index=True)  # inject a dup
    h = assess_source(df, "manifold", now=now)
    joined = " ".join(h.issues)
    assert "stale" in joined
    assert "duplicate" in joined


def test_empty_source():
    h = assess_source(pd.DataFrame(), "polymarket")
    assert h.n_quotes == 0
    assert h.reliability_grade == "F"


def test_empty_clean_but_all_raw_excluded_as_combos_explains_why(tmp_path):
    # Regression: if EVERY fetched Kalshi market is an excluded combo basket,
    # clean/ never gets written at all. The empty-df path must still surface
    # the combo rate from raw/ rather than a bare, unhelpful "no data".
    import json

    snap = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    raw_df = pd.DataFrame(
        {
            "source": ["kalshi"] * 3,
            "market_id": ["m0", "m1", "m2"],
            "snapshot_ts": [snap] * 3,
            "payload": [json.dumps({"ticker": "KXMVESPORTSMULTIGAMEEXTENDED-X"})] * 3,
        }
    )
    h = assess_source(pd.DataFrame(), "kalshi", raw_df=raw_df)
    assert h.n_quotes == 0
    assert h.combo_excluded_rate == 1.0
    assert "100% of raw payloads were excluded combo/parlay" in h.issues[0]


def test_prob_violation_surfaces():
    now = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    df = _quote_frame(now - timedelta(minutes=1), n=2)
    df.loc[0, "implied_prob"] = 1.4
    h = assess_source(df, "kalshi", now=now)
    assert h.prob_violations == 1
    assert any("outside" in i for i in h.issues)


# --------------------------------------------------------------------------- #
# Report round-trip on a real lake
# --------------------------------------------------------------------------- #


def test_report_roundtrip(tmp_path):
    root = tmp_path
    snap = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    d = root / "clean" / "source=kalshi" / "date=2026-06-18"
    d.mkdir(parents=True)
    _quote_frame(snap, n=4).to_parquet(d / "snap.parquet", index=False)

    report = compute_quality_report(data_root=str(root))
    assert list(report["source"]) == ["kalshi"]
    assert int(report.loc[0, "n_quotes"]) == 4
    assert report.loc[0, "stale_price_rate"] == 0.0
    assert report.loc[0, "combo_excluded_rate"] == 0.0

    path = write_quality_report(data_root=str(root))
    assert path is not None and path.exists()
    back = read_quality_report(data_root=str(root))
    assert "generated_at" in back.columns
    assert "stale_price_rate" in back.columns
    assert "combo_excluded_rate" in back.columns


def test_report_surfaces_stale_prices_and_combo_exclusions(tmp_path):
    root = tmp_path
    snap = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)
    clean_dir = root / "clean" / "source=kalshi" / "date=2026-06-18"
    clean_dir.mkdir(parents=True)
    clean = _quote_frame(snap, n=4)
    clean["price_is_stale"] = [True, True, False, False]  # 50% stale
    clean.to_parquet(clean_dir / "snap.parquet", index=False)

    raw_dir = root / "raw" / "source=kalshi" / "date=2026-06-18"
    raw_dir.mkdir(parents=True)
    import json

    raw = pd.DataFrame(
        {
            "source": ["kalshi"] * 3,
            "market_id": ["m0", "m1", "m2"],
            "snapshot_ts": [snap] * 3,
            "payload": [
                json.dumps({"ticker": "KXHIGHNY-26JUN17-B82.5"}),
                json.dumps({"ticker": "KXMVESPORTSMULTIGAMEEXTENDED-X"}),
                json.dumps({"ticker": "KXMVECROSSCATEGORY-Y"}),
            ],
        }
    )
    raw.to_parquet(raw_dir / "snap.parquet", index=False)

    report = compute_quality_report(data_root=str(root))
    row = report.iloc[0]
    assert row["stale_price_rate"] == 0.5
    assert row["combo_excluded_rate"] == pytest.approx(2 / 3, abs=1e-3)
    joined = row["issues"]
    assert "stale-fallback" in joined
    assert "excluded combo/parlay" in joined


def test_empty_lake_returns_empty(tmp_path):
    assert compute_quality_report(data_root=str(tmp_path)).empty
    assert write_quality_report(data_root=str(tmp_path)) is None
    assert read_quality_report(data_root=str(tmp_path)).empty
