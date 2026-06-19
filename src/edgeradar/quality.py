"""Data quality & observability: turn the raw lake into a health report.

This module answers the question a reviewer should always ask before trusting a
dashboard: *can I trust the data underneath it?* It scans the clean Parquet lake
and produces one row per source describing how fresh, complete, and internally
consistent that feed is, plus a blended 0–100 reliability score (from
``analytics.score_source_reliability``).

The checks are deliberately the boring, high-value ones that catch real
production breakage:

- Freshness        — minutes since the source's most recent snapshot.
- Volume           — quote and distinct-market counts in the latest snapshot.
- Null rate        — fraction of NULLs in the field that matters (implied_prob).
- Duplicate rate   — rows sharing a natural key (ingestion idempotency check).
- Probability bounds — implied_prob values outside [0, 1] (math / parsing bugs).
- Snapshot consistency — the latest snapshot's volume vs the source's typical
  volume, which catches a feed that silently half-broke.
- Stale-price fallback rate — fraction of quotes whose implied_prob came from a
  flagged last-traded-price fallback rather than a live two-sided quote (see
  adapters/kalshi.py); never silent, always visible here.
- Combo-market exclusion rate — fraction of the RAW payloads landed that were
  MVE combo/parlay baskets the adapter excluded before normalizing (the live
  Kalshi-feed gotcha that originally motivated this report — see FINDINGS.md).
  0.0 for sources with no such concept.

``write_quality_report`` persists the table to ``data/quality/data_quality.parquet``
so the dashboard and CI can display it without re-scanning. Everything degrades
gracefully on an empty lake (returns an empty frame, never raises).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from edgeradar.adapters.kalshi import is_combo_market
from edgeradar.analytics import score_source_reliability
from edgeradar.config import get_settings

QUALITY_DIR = "quality"
QUALITY_FILE = "data_quality.parquet"

# A snapshot whose volume is below this fraction of the source's median is flagged
# as a possible partial-ingest, even if nothing errored.
PARTIAL_INGEST_RATIO = 0.5

# The field whose presence we treat as the completeness signal for a quote.
COMPLETENESS_FIELD = "implied_prob"


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested directly)
# --------------------------------------------------------------------------- #


def null_rate(series: pd.Series) -> float:
    """Fraction of NULL/NaN values in a column (0.0 for an empty column)."""
    if series is None or len(series) == 0:
        return 0.0
    return float(series.isna().mean())


def duplicate_rate(df: pd.DataFrame, key: list[str]) -> float:
    """Fraction of rows that are duplicates on the natural key.

    A healthy, idempotent feed has a duplicate rate of 0: re-ingesting the same
    snapshot must not create new rows. A nonzero rate is a real data bug.
    """
    if df is None or df.empty:
        return 0.0
    present = [c for c in key if c in df.columns]
    if not present:
        return 0.0
    return float(df.duplicated(subset=present).mean())


def prob_bounds_violations(series: pd.Series) -> int:
    """Count probability values that fall outside the valid [0, 1] interval."""
    if series is None or len(series) == 0:
        return 0
    s = pd.to_numeric(series, errors="coerce").dropna()
    return int(((s < 0.0) | (s > 1.0)).sum())


def stale_price_rate(series: pd.Series) -> float:
    """Fraction of quotes flagged `price_is_stale` (a fallback price, not a live
    quote — see adapters/kalshi.py). 0.0 for an empty/missing column, never None,
    so it's always visible rather than silently absent."""
    if series is None or len(series) == 0:
        return 0.0
    return float(series.fillna(False).astype(bool).mean())


def combo_exclusion_rate(payloads: pd.Series) -> float:
    """Fraction of raw payloads that are excluded MVE combo/parlay baskets.

    `payloads` is the raw zone's `payload` column (JSON strings, as landed by
    `storage.write_raw`) or already-decoded dicts. 0.0 for sources with no such
    concept (the combo predicate just never matches their payload shape) — not a
    special case, the same `is_combo_market` check used at ingestion time.
    """
    if payloads is None or len(payloads) == 0:
        return 0.0

    def _is_combo(p: object) -> bool:
        try:
            d = json.loads(p) if isinstance(p, str) else p
            return bool(is_combo_market(d)) if isinstance(d, dict) else False
        except (TypeError, ValueError):
            return False

    return float(payloads.map(_is_combo).mean())


# --------------------------------------------------------------------------- #
# Per-source report
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceHealth:
    """One source's health snapshot. Mirrors a row of the quality report frame."""

    source: str
    n_quotes: int
    n_markets: int
    last_snapshot: pd.Timestamp | None
    age_minutes: float | None
    null_rate: float
    duplicate_rate: float
    prob_violations: int
    partial_ingest: bool
    stale_price_rate: float
    combo_excluded_rate: float
    reliability_score: float
    reliability_grade: str
    issues: list[str]


def _read_raw_files(root: Path, source: str) -> pd.DataFrame:
    """Read a single source's raw-zone files (payload as landed JSON strings)."""
    files = sorted(root.glob(f"raw/source={source}/**/*.parquet"))
    frames = [pd.read_parquet(f) for f in files]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _read_source_files(root: Path, source: str) -> pd.DataFrame:
    """Read a single source's clean files WITHOUT dedup (so we can measure dups)."""
    files = sorted(root.glob(f"clean/source={source}/**/*.parquet"))
    frames = [pd.read_parquet(f) for f in files]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _discover_sources(root: Path) -> list[str]:
    """Sources with EITHER clean or raw data.

    Including raw-only sources matters: if every fetched record gets excluded
    (e.g. a Kalshi pull that returned 100% combo markets), `clean/` never gets a
    directory at all. Without this, that source would silently vanish from the
    report instead of showing up as "0 quotes, 100% combo-excluded, grade F".
    """
    clean = {p.name.split("=", 1)[1] for p in root.glob("clean/source=*") if "=" in p.name}
    raw = {p.name.split("=", 1)[1] for p in root.glob("raw/source=*") if "=" in p.name}
    return sorted(clean | raw)


def assess_source(
    df: pd.DataFrame,
    source: str,
    *,
    now: datetime | None = None,
    brier: float | None = None,
    raw_df: pd.DataFrame | None = None,
) -> SourceHealth:
    """Build a SourceHealth from one source's (non-deduped) quote frame.

    `raw_df` (the raw zone's payloads, if available) drives `combo_excluded_rate`
    — it's optional because not every caller has the raw lake handy, and the
    rate is simply 0.0 without it rather than a hard requirement.
    """
    now = now or datetime.now(timezone.utc)
    key = ["source", "market_id", "outcome", "snapshot_ts"]

    if df.empty:
        # Even with zero CLEAN quotes, raw payloads may exist — e.g. every fetched
        # Kalshi market was an excluded combo basket. Surface that rather than a
        # bare "no data" (the combo rate is exactly why there's no clean data).
        combo_rate = 0.0
        if raw_df is not None and not raw_df.empty and "payload" in raw_df:
            combo_rate = combo_exclusion_rate(raw_df["payload"])
        issues = ["no data for this source"]
        if combo_rate > 0:
            issues = [
                f"{combo_rate * 100:.0f}% of raw payloads were excluded combo/parlay "
                "markets, leaving 0 usable quotes"
            ]
        return SourceHealth(
            source=source,
            n_quotes=0,
            n_markets=0,
            last_snapshot=None,
            age_minutes=None,
            null_rate=1.0,
            duplicate_rate=0.0,
            prob_violations=0,
            partial_ingest=False,
            stale_price_rate=0.0,
            combo_excluded_rate=round(combo_rate, 4),
            reliability_score=0.0,
            reliability_grade="F",
            issues=issues,
        )

    snaps = pd.to_datetime(df["snapshot_ts"], errors="coerce", utc=True)
    last = snaps.max()
    age_min = None if pd.isna(last) else max((now - last.to_pydatetime()).total_seconds() / 60, 0)

    latest_mask = snaps == last
    latest = df[latest_mask]
    n_quotes = int(len(latest))
    n_markets = int(latest["market_id"].nunique()) if "market_id" in latest else 0

    nr = null_rate(latest[COMPLETENESS_FIELD]) if COMPLETENESS_FIELD in latest else 1.0
    dup = duplicate_rate(df, key)
    viol = prob_bounds_violations(latest[COMPLETENESS_FIELD]) if COMPLETENESS_FIELD in latest else 0
    stale = stale_price_rate(latest["price_is_stale"]) if "price_is_stale" in latest else 0.0

    combo_rate = 0.0
    if raw_df is not None and not raw_df.empty and "payload" in raw_df:
        raw_snaps = pd.to_datetime(raw_df["snapshot_ts"], errors="coerce", utc=True)
        raw_latest = raw_df[raw_snaps == raw_snaps.max()]
        combo_rate = combo_exclusion_rate(raw_latest["payload"])

    # Partial-ingest check: latest snapshot vs the source's median per-snapshot volume.
    per_snap = df.groupby(snaps).size()
    median_vol = float(per_snap.median()) if len(per_snap) else 0.0
    partial = bool(median_vol > 0 and n_quotes < PARTIAL_INGEST_RATIO * median_vol)

    rel = score_source_reliability(source, age_minutes=age_min, completeness=1.0 - nr, brier=brier)

    issues: list[str] = []
    if age_min is not None and age_min > 60:
        issues.append(f"stale: last snapshot {age_min:.0f} min ago")
    if dup > 0:
        issues.append(f"{dup * 100:.1f}% duplicate rows (idempotency broken)")
    if nr > 0.1:
        issues.append(f"{nr * 100:.0f}% of {COMPLETENESS_FIELD} is null")
    if viol > 0:
        issues.append(f"{viol} probability values outside [0,1]")
    if partial:
        issues.append("latest snapshot is unusually small (possible partial ingest)")
    if stale > 0.05:
        issues.append(f"{stale * 100:.0f}% of prices are stale-fallback, not live quotes")
    if combo_rate > 0:
        issues.append(f"{combo_rate * 100:.0f}% of raw payloads were excluded combo/parlay markets")

    return SourceHealth(
        source=source,
        n_quotes=n_quotes,
        n_markets=n_markets,
        last_snapshot=None if pd.isna(last) else last,
        age_minutes=age_min,
        null_rate=round(nr, 4),
        duplicate_rate=round(dup, 4),
        prob_violations=viol,
        partial_ingest=partial,
        stale_price_rate=round(stale, 4),
        combo_excluded_rate=round(combo_rate, 4),
        reliability_score=rel.score,
        reliability_grade=rel.grade,
        issues=issues or ["healthy"],
    )


def compute_quality_report(
    *, data_root: str | None = None, now: datetime | None = None
) -> pd.DataFrame:
    """Scan the clean lake and return one health row per source (empty frame if none)."""
    root = Path(data_root or get_settings().data_root)
    sources = _discover_sources(root)
    rows = []
    for src in sources:
        health = assess_source(
            _read_source_files(root, src), src, now=now, raw_df=_read_raw_files(root, src)
        )
        rows.append(
            {
                "source": health.source,
                "n_quotes": health.n_quotes,
                "n_markets": health.n_markets,
                "last_snapshot": health.last_snapshot,
                "age_minutes": health.age_minutes,
                "null_rate": health.null_rate,
                "duplicate_rate": health.duplicate_rate,
                "prob_violations": health.prob_violations,
                "partial_ingest": health.partial_ingest,
                "stale_price_rate": health.stale_price_rate,
                "combo_excluded_rate": health.combo_excluded_rate,
                "reliability_score": health.reliability_score,
                "reliability_grade": health.reliability_grade,
                "issues": "; ".join(health.issues),
            }
        )
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("source").reset_index(drop=True)


def write_quality_report(*, data_root: str | None = None) -> Path | None:
    """Compute the report and persist it to data/quality/data_quality.parquet.

    Returns the path written, or None when the lake is empty.
    """
    root = Path(data_root or get_settings().data_root)
    df = compute_quality_report(data_root=str(root))
    if df.empty:
        return None
    out_dir = root / QUALITY_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / QUALITY_FILE
    # generated_at lets the dashboard show when the report was last refreshed.
    df = df.copy()
    df["generated_at"] = datetime.now(timezone.utc)
    df.to_parquet(path, index=False)
    return path


def read_quality_report(*, data_root: str | None = None) -> pd.DataFrame:
    """Read the persisted quality report (empty frame if it doesn't exist yet)."""
    root = Path(data_root or get_settings().data_root)
    path = root / QUALITY_DIR / QUALITY_FILE
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)
