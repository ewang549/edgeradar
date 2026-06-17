"""Small shared helpers for adapters (time parsing + dry-run constant)."""

from __future__ import annotations

from datetime import datetime, timezone

# Fixed timestamp used for ALL --dry-run fetches so offline runs are deterministic
# and idempotent: re-running a dry-run overwrites the same snapshot file rather
# than creating a "new" snapshot each time.
DRY_RUN_TS = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def ms_to_dt(ms: int | float | None) -> datetime | None:
    """Convert epoch milliseconds (UTC) to a timezone-aware datetime."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def parse_iso(s: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp (handling a trailing 'Z') to UTC-aware datetime."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
