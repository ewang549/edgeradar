"""Producer: fetch raw records via an adapter and publish them to the topic.

`confluent_kafka` is imported lazily so the pure modules (and their tests) don't
require the native Kafka library to be installed.
"""

from __future__ import annotations

from edgeradar.config import get_settings
from edgeradar.ingest import REGISTRY
from edgeradar.streaming.serde import encode_raw, message_key


def _make_producer(bootstrap: str):
    from confluent_kafka import Producer

    return Producer({"bootstrap.servers": bootstrap, "enable.idempotence": True})


def produce_source(source: str, *, dry_run: bool = False) -> int:
    """Fetch one source's raw records and publish them to the raw topic.

    Returns the number of messages produced. Honors --dry-run so you can drive the
    whole streaming path offline from saved sample responses.
    """
    settings = get_settings()
    if source == "all":
        sources = list(REGISTRY)
    elif source in REGISTRY:
        sources = [source]
    else:
        raise ValueError(f"Unknown source {source!r}. Known: {', '.join(REGISTRY)} or 'all'.")

    producer = _make_producer(settings.kafka_bootstrap_servers)
    topic = settings.kafka_topic_raw

    count = 0
    for slug in sources:
        adapter = REGISTRY[slug](dry_run=dry_run)
        for record in adapter.fetch():
            producer.produce(topic, key=message_key(record), value=encode_raw(record))
            count += 1
        producer.poll(0)  # serve delivery callbacks
    producer.flush()
    return count
