"""Resolution diagnostics: explain WHY markets did or didn't match.

`entity_resolution.resolve()` produces an `event_map` and `candidate_pairs`, but
when cross-platform matching comes back empty there was previously no way to
tell whether that's a real bug (e.g. a source adapter only returning combo
markets) or genuine non-overlap (e.g. two platforms simply cover different
cities). This module turns the same inputs `resolve()` already computed into a
small, persisted report — counts of markets per source per category, how many
landed in each (category, entity) blocking sub-block, how many candidate pairs
were scored, and the score distribution of near-misses — plus a plain-language
explanation for the dashboard's Resolution page.

Mirrors `quality.py`'s shape on purpose: pure functions over DataFrames (unit
testable without the lake), persisted to Parquet, degrading gracefully on
empty input rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from edgeradar.config import get_settings

DIAGNOSTICS_DIR = "marts"
SUMMARY_FILE = "resolution_diagnostics.parquet"
BLOCKS_FILE = "resolution_diagnostics_blocks.parquet"

# A confidence within this much of the match threshold counts as a "near miss"
# for the summary stats (mirrors entity_resolution.REVIEW_BAND).
NEAR_MISS_BAND = 0.12


@dataclass
class ResolutionDiagnostics:
    """One resolution run's diagnosis of what matched, what didn't, and why."""

    blocks: pd.DataFrame  # category, source, n_markets, n_with_entity
    n_markets: int = 0
    n_categories: int = 0
    n_pairs_scored: int = 0
    n_pairs_matched: int = 0
    n_near_miss: int = 0
    near_miss_mean: float | None = None
    near_miss_max: float | None = None
    n_cross_platform: int = 0
    reasons: list[str] = field(default_factory=list)
    generated_at: datetime | None = None


def _category_source_overlap_gaps(blocks: pd.DataFrame) -> list[str]:
    """Plain-language notes on which sources never share a category with anyone."""
    if blocks.empty:
        return []
    by_cat = blocks.groupby("category")["source"].agg(lambda s: sorted(set(s)))
    multi_source_cats = {cat for cat, srcs in by_cat.items() if len(srcs) > 1}
    notes = []
    for src in sorted(blocks["source"].unique()):
        src_cats = set(blocks.loc[blocks["source"] == src, "category"])
        if src_cats and not (src_cats & multi_source_cats):
            cats = ", ".join(sorted(src_cats))
            notes.append(f"{src} only contributed markets in {cats!r}; no other source covers them")
    return notes


def explain(
    blocks: pd.DataFrame,
    candidate_pairs: pd.DataFrame,
    *,
    n_markets: int,
    n_cross_platform: int,
) -> list[str]:
    """Plain-language reasons for the resolution outcome — esp. why 0 events matched."""
    if n_markets == 0:
        return ["no markets ingested yet — run `edgeradar ingest` (or `--dry-run`) first"]

    if n_cross_platform > 0:
        return [f"{n_cross_platform} cross-platform event(s) matched — resolution is working"]

    reasons: list[str] = []
    gaps = _category_source_overlap_gaps(blocks)
    if gaps:
        reasons.extend(gaps)

    by_cat = blocks.groupby("category")["source"].nunique() if not blocks.empty else pd.Series()
    if (by_cat >= 2).any():
        n_scored = int(len(candidate_pairs))
        if n_scored == 0:
            reasons.append(
                "categories with multiple sources exist, but no candidate pairs were "
                "scored (likely no shared extracted entity — see entity sub-blocking)"
            )
        else:
            matched = int((candidate_pairs["decision"].isin(["match", "manual-match"])).sum())
            if matched == 0:
                best = float(candidate_pairs["confidence"].max())
                reasons.append(
                    f"{n_scored} pair(s) scored across categories with multiple sources, "
                    f"all below the match threshold (closest score: {best:.2f})"
                )
    else:
        reasons.append(
            "no category had ≥2 sources contributing markets — there was nothing to compare"
        )

    return reasons or ["no cross-platform events matched, and no specific cause was identified"]


def compute_resolution_diagnostics(
    markets: pd.DataFrame, candidate_pairs: pd.DataFrame, *, n_cross_platform: int
) -> ResolutionDiagnostics:
    """Build a `ResolutionDiagnostics` from `resolve()`'s own intermediate data.

    `markets` is `entity_resolution.load_latest_markets()`'s output (one row per
    market, with `category`/`entities` columns); `candidate_pairs` is
    `ResolveResult.candidate_pairs`. Never raises on empty input.
    """
    if markets is None or markets.empty:
        return ResolutionDiagnostics(
            blocks=pd.DataFrame(columns=["category", "source", "n_markets", "n_with_entity"]),
            reasons=explain(pd.DataFrame(), pd.DataFrame(), n_markets=0, n_cross_platform=0),
            generated_at=datetime.now(timezone.utc),
        )

    m = markets.copy()
    m["has_entity"] = m["entities"].map(bool) if "entities" in m else False
    blocks = (
        m.groupby(["category", "source"])
        .agg(n_markets=("market_id", "size"), n_with_entity=("has_entity", "sum"))
        .reset_index()
    )

    n_scored = int(len(candidate_pairs)) if candidate_pairs is not None else 0
    n_matched = (
        int(candidate_pairs["decision"].isin(["match", "manual-match"]).sum()) if n_scored else 0
    )
    near_miss = (
        candidate_pairs.loc[candidate_pairs["decision"] == "no-match", "confidence"]
        if n_scored
        else pd.Series(dtype=float)
    )

    cp = candidate_pairs if candidate_pairs is not None else pd.DataFrame()
    return ResolutionDiagnostics(
        blocks=blocks,
        n_markets=int(len(m)),
        n_categories=int(m["category"].nunique()),
        n_pairs_scored=n_scored,
        n_pairs_matched=n_matched,
        n_near_miss=int(len(near_miss)),
        near_miss_mean=round(float(near_miss.mean()), 4) if len(near_miss) else None,
        near_miss_max=round(float(near_miss.max()), 4) if len(near_miss) else None,
        n_cross_platform=n_cross_platform,
        reasons=explain(blocks, cp, n_markets=int(len(m)), n_cross_platform=n_cross_platform),
        generated_at=datetime.now(timezone.utc),
    )


def write_resolution_diagnostics(
    diag: ResolutionDiagnostics, *, data_root: str | None = None
) -> Path | None:
    """Persist the diagnostics (summary + per-block counts) as Parquet."""
    root = Path(data_root or get_settings().data_root)
    out_dir = root / DIAGNOSTICS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    blocks_path = out_dir / BLOCKS_FILE
    diag.blocks.to_parquet(blocks_path, index=False)

    summary = pd.DataFrame(
        [
            {
                "n_markets": diag.n_markets,
                "n_categories": diag.n_categories,
                "n_pairs_scored": diag.n_pairs_scored,
                "n_pairs_matched": diag.n_pairs_matched,
                "n_near_miss": diag.n_near_miss,
                "near_miss_mean": diag.near_miss_mean,
                "near_miss_max": diag.near_miss_max,
                "n_cross_platform": diag.n_cross_platform,
                "reasons": "; ".join(diag.reasons),
                "generated_at": diag.generated_at or datetime.now(timezone.utc),
            }
        ]
    )
    summary_path = out_dir / SUMMARY_FILE
    summary.to_parquet(summary_path, index=False)
    return summary_path


def read_resolution_diagnostics(
    *, data_root: str | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Read back (summary, blocks) — empty frames if the report hasn't been written."""
    root = Path(data_root or get_settings().data_root)
    summary_path = root / DIAGNOSTICS_DIR / SUMMARY_FILE
    blocks_path = root / DIAGNOSTICS_DIR / BLOCKS_FILE
    summary = pd.read_parquet(summary_path) if summary_path.exists() else pd.DataFrame()
    blocks = pd.read_parquet(blocks_path) if blocks_path.exists() else pd.DataFrame()
    return summary, blocks
