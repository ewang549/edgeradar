"""Small shared helpers for adapters (time parsing + dry-run constant)."""

from __future__ import annotations

from datetime import datetime, timezone

# Fixed timestamp used for ALL --dry-run fetches so offline runs are deterministic
# and idempotent: re-running a dry-run overwrites the same snapshot file rather
# than creating a "new" snapshot each time.
DRY_RUN_TS = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

# pandas datetime64[ns] only spans ~1677..2262. Some platforms use far-future
# "never closes" sentinels (e.g. year 5555), which overflow pandas. Clamp anything
# outside a safe window to None ("unknown close date") so it never enters the lake.
_SAFE_MIN = datetime(1700, 1, 1, tzinfo=timezone.utc)
_SAFE_MAX = datetime(2261, 12, 31, tzinfo=timezone.utc)


def _safe(dt: datetime | None) -> datetime | None:
    """Return dt only if it's within pandas-representable bounds, else None."""
    if dt is None:
        return None
    return dt if _SAFE_MIN <= dt <= _SAFE_MAX else None


def ms_to_dt(ms: int | float | None) -> datetime | None:
    """Convert epoch milliseconds (UTC) to a timezone-aware datetime (bounded)."""
    if ms is None:
        return None
    try:
        dt = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return _safe(dt)


def parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (handling a trailing 'Z') to UTC-aware datetime (bounded)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _safe(dt)
