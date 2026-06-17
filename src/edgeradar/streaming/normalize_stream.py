"""Normalize a batch of raw records into clean quotes (broker-agnostic).

Routes each raw record to its source's adapter `normalize()` (reusing the exact
same logic as batch ingestion), then applies the fee-adjustment hook. Pure and
unit-tested; the consumer calls this on whatever it polled off the topic.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from edgeradar.ingest import REGISTRY
from edgeradar.models import MarketQuote, RawRecord


def normalize_records(records: Iterable[RawRecord]) -> list[MarketQuote]:
    """Group raw records by source and normalize each group.

    `normalize()` already computes implied_prob, the fair point estimate
    (fee_adj_prob), the spread, and the trade_cost via the fee model, so both the
    batch and streaming paths produce identical, cost-annotated quotes.
    """
    by_source: dict[str, list[RawRecord]] = defaultdict(list)
    for rec in records:
        by_source[rec.source].append(rec)

    quotes: list[MarketQuote] = []
    for source, recs in by_source.items():
        adapter_cls = REGISTRY.get(source)
        if adapter_cls is None:
            continue  # unknown source: skip rather than crash the consumer
        # normalize() is pure; the dry_run flag is irrelevant here (no fetch).
        adapter = adapter_cls(dry_run=True)
        quotes.extend(adapter.normalize(recs))
    return quotes
