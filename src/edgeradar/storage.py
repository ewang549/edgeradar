"""Local data-lake writer/reader (Parquet, partitioned by source/date).

Phase 1 lands data on the local filesystem under `data/` (mounted into the app
container). The lake has two zones:

    data/raw/source=<s>/date=<YYYY-MM-DD>/snapshot=<ts>.parquet
        raw API payloads, stored verbatim as JSON strings (auditable; lets us
        re-derive normalized values later if our math changes).

    data/clean/source=<s>/date=<YYYY-MM-DD>/snapshot=<ts>.parquet
        normalized MarketQuote rows ready for the warehouse.

Idempotency: the filename is derived deterministically from the snapshot
timestamp, so re-running the *same* snapshot (e.g. a --dry-run, which uses a
fixed timestamp) overwrites the same file instead of appending a duplicate.
Within a file we also drop duplicate natural keys. Distinct live runs use a new
snapshot_ts and are kept side by side on purpose — that's the time series, not a
duplicate. (MinIO/S3 becomes the backing store in a later phase; this same
interface will gain an S3 writer.)
"""

from __future__ import annotations

import json
import warnings
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pandas as pd

from edgeradar.config import get_settings
from edgeradar.models import MarketQuote, RawRecord


def _safe_ts(ts: datetime) -> str:
    """Filesystem-safe UTC timestamp stamp, e.g. 20260617T080000Z."""
    return ts.strftime("%Y%m%dT%H%M%SZ")


def _zone_path(zone: str, source: str, snapshot_ts: datetime, *, data_root: str | None) -> Path:
    root = Path(data_root or get_settings().data_root)
    date_str = snapshot_ts.strftime("%Y-%m-%d")
    return (
        root
        / zone
        / f"source={source}"
        / f"date={date_str}"
        / f"snapshot={_safe_ts(snapshot_ts)}.parquet"
    )


def write_raw(records: Sequence[RawRecord], *, data_root: str | None = None) -> Path | None:
    """Persist raw payloads to the raw zone. Returns the file path (or None if empty)."""
    if not records:
        return None
    source = records[0].source
    snapshot_ts = records[0].snapshot_ts
    rows = [
        {
            "source": r.source,
            "market_id": r.market_id,
            "snapshot_ts": r.snapshot_ts,
            "payload": json.dumps(r.payload, default=str),
        }
        for r in records
    ]
    df = pd.DataFrame(rows).drop_duplicates(subset=["source", "market_id", "snapshot_ts"])
    path = _zone_path("raw", source, snapshot_ts, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return path


def write_quotes(quotes: Sequence[MarketQuote], *, data_root: str | None = None) -> Path | None:
    """Persist normalized quotes to the clean zone (deduped on natural key)."""
    if not quotes:
        return None
    source = quotes[0].source
    snapshot_ts = quotes[0].snapshot_ts
    df = pd.DataFrame(q.model_dump() for q in quotes)
    # price is a Decimal; store as float for Parquet/warehouse friendliness.
    df["price"] = df["price"].astype(float)
    df = df.drop_duplicates(subset=["source", "market_id", "outcome", "snapshot_ts"])
    path = _zone_path("clean", source, snapshot_ts, data_root=data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return path


def write_quotes_grouped(
    quotes: Sequence[MarketQuote], *, data_root: str | None = None
) -> list[Path]:
    """Write a mixed batch of quotes, grouping by (source, snapshot_ts).

    The streaming consumer may hold quotes from several sources/snapshots at once;
    each (source, snapshot_ts) group maps to one deterministic Parquet file, so
    re-processing the same group overwrites rather than duplicates.
    """
    groups: dict[tuple[str, datetime], list[MarketQuote]] = {}
    for q in quotes:
        groups.setdefault((q.source, q.snapshot_ts), []).append(q)
    paths: list[Path] = []
    for group in groups.values():
        path = write_quotes(group, data_root=data_root)
        if path is not None:
            paths.append(path)
    return paths


def read_quotes(*, source: str | None = None, data_root: str | None = None) -> pd.DataFrame:
    """Read all clean quotes back as a single DataFrame (deduped on natural key).

    Convenience for tests and quick inspection before the dbt warehouse exists.
    """
    root = Path(data_root or get_settings().data_root)
    pattern = f"clean/source={source}/**/*.parquet" if source else "clean/**/*.parquet"
    files = sorted(root.glob(pattern))
    if not files:
        return pd.DataFrame()
    frames = [pd.read_parquet(f) for f in files]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame()
    # Some per-source files have all-NA columns (e.g. Manifold has no `spread`),
    # which triggers a pandas concat FutureWarning about dtype inference. The
    # behavior is fine for us (we want the union of columns), so silence it.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates(subset=["source", "market_id", "outcome", "snapshot_ts"])
