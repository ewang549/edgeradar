"""Consumer: read raw records off the topic, normalize, and land clean Parquet.

Polls until no new message arrives for `idle_timeout` seconds (a drain pattern
that's convenient for batch-style runs and demos), then normalizes the whole
batch and writes the clean zone. Re-running produce + consume with the same
(deterministic, in --dry-run) snapshot overwrites the same files, so the clean
zone stays duplicate-free across reruns.

`confluent_kafka` is imported lazily (see producer.py for why).
"""

from __future__ import annotations

from dataclasses import dataclass

from edgeradar.config import get_settings
from edgeradar.storage import write_quotes_grouped
from edgeradar.streaming.normalize_stream import normalize_records
from edgeradar.streaming.serde import decode_raw


@dataclass
class ConsumeResult:
    messages: int
    quotes: int
    files: list[str]


def consume_and_land(
    *, idle_timeout: float = 5.0, max_messages: int | None = None
) -> ConsumeResult:
    """Drain the raw topic, normalize, and write clean Parquet. Returns a summary."""
    from confluent_kafka import Consumer

    settings = get_settings()
    consumer = Consumer(
        {
            "bootstrap.servers": settings.kafka_bootstrap_servers,
            "group.id": settings.kafka_consumer_group,
            "auto.offset.reset": "earliest",
            "enable.auto.commit": True,
        }
    )
    consumer.subscribe([settings.kafka_topic_raw])

    records = []
    try:
        while True:
            msg = consumer.poll(timeout=idle_timeout)
            if msg is None:
                break  # idle: assume the batch is drained
            if msg.error():
                continue
            records.append(decode_raw(msg.value()))
            if max_messages is not None and len(records) >= max_messages:
                break
    finally:
        consumer.close()

    quotes = normalize_records(records)
    paths = write_quotes_grouped(quotes)
    return ConsumeResult(
        messages=len(records),
        quotes=len(quotes),
        files=[str(p) for p in paths],
    )
