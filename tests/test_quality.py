"""Tests for the data-quality / observability module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from edgeradar.quality import (
    assess_source,
    compute_quality_report,
    duplicate_rate,
    null_rate,
    prob_bounds_violations,
    read_quality_report,
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

    path = write_quality_report(data_root=str(root))
    assert path is not None and path.exists()
    back = read_quality_report(data_root=str(root))
    assert "generated_at" in back.columns
    assert int(back.loc[0, "n_quotes"]) == 4


def test_empty_lake_returns_empty(tmp_path):
    assert compute_quality_report(data_root=str(tmp_path)).empty
    assert write_quality_report(data_root=str(tmp_path)) is None
    assert read_quality_report(data_root=str(tmp_path)).empty
