"""(De)serialization for messages on the wire.

Each Kafka message is one raw record: a JSON object with the source, the platform
market id, our UTC fetch timestamp, and the verbatim payload. Keeping this pure
(no broker imports) makes it trivially testable and reusable.
"""

from __future__ import annotations

import json
from datetime import datetime

from edgeradar.models import RawRecord


def encode_raw(record: RawRecord) -> bytes:
    """Serialize a RawRecord to UTF-8 JSON bytes for the topic value."""
    obj = {
        "source": record.source,
        "market_id": record.market_id,
        "snapshot_ts": record.snapshot_ts.isoformat(),
        "payload": record.payload,
    }
    return json.dumps(obj, default=str).encode("utf-8")


def decode_raw(data: bytes) -> RawRecord:
    """Deserialize topic-value bytes back into a RawRecord."""
    obj = json.loads(data.decode("utf-8"))
    return RawRecord(
        source=obj["source"],
        market_id=obj["market_id"],
        snapshot_ts=datetime.fromisoformat(obj["snapshot_ts"]),
        payload=obj["payload"],
    )


def message_key(record: RawRecord) -> bytes:
    """Partition key = source:market_id, so a market's quotes stay ordered."""
    return f"{record.source}:{record.market_id}".encode()
