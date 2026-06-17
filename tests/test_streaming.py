"""Phase 3 tests: the broker-agnostic streaming logic.

These cover serialization, source-routed normalization + fee hook, and idempotent
grouped writes. The Kafka transport itself (producer/consumer against Redpanda) is
verified end-to-end via `make produce` / `make consume`; it needs no native Kafka
library here, so these run anywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone

from edgeradar.adapters.manifold import ManifoldAdapter
from edgeradar.models import RawRecord
from edgeradar.storage import read_quotes, write_quotes_grouped
from edgeradar.streaming.normalize_stream import normalize_records
from edgeradar.streaming.serde import decode_raw, encode_raw, message_key


def _sample_raw() -> list[RawRecord]:
    # Reuse the committed dry-run fixtures via the adapter's offline fetch.
    return list(ManifoldAdapter(dry_run=True).fetch())


def test_serde_round_trip():
    rec = RawRecord(
        source="manifold",
        market_id="abc123",
        snapshot_ts=datetime(2025, 1, 1, tzinfo=timezone.utc),
        payload={"question": "Will X happen?", "probability": 0.42},
    )
    back = decode_raw(encode_raw(rec))
    assert back.source == rec.source
    assert back.market_id == rec.market_id
    assert back.snapshot_ts == rec.snapshot_ts
    assert back.payload == rec.payload
    assert message_key(rec) == b"manifold:abc123"


def test_normalize_records_routes_and_fee_adjusts():
    quotes = normalize_records(_sample_raw())
    # 4 active binary markets survive (resolved + multi-choice filtered out).
    assert len(quotes) == 4
    for q in quotes:
        assert q.source == "manifold"
        # Placeholder fee hook: fee_adj_prob mirrors implied_prob (identity for now).
        assert q.fee_adj_prob == q.implied_prob


def test_grouped_write_is_idempotent(tmp_path):
    quotes = normalize_records(_sample_raw())
    write_quotes_grouped(quotes, data_root=str(tmp_path))
    first = len(read_quotes(data_root=str(tmp_path)))
    write_quotes_grouped(quotes, data_root=str(tmp_path))  # same snapshot -> overwrite
    second = len(read_quotes(data_root=str(tmp_path)))
    assert first == second == 4
