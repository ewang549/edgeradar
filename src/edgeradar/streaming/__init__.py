"""Streaming layer (Phase 3).

Adapters publish raw quotes to a Kafka/Redpanda topic (producer); a separate
consumer normalizes, fee-adjusts, dedupes, and writes the clean Parquet zone.
The serialization (`serde`) and normalization (`normalize_stream`) logic is pure
and unit-tested; only `producer`/`consumer` touch the broker.
"""
