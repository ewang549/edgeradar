"""Ingestion runner: drive adapters and land raw + clean Parquet.

The adapter REGISTRY maps a source slug to its adapter class. Adding a new source
later means importing its class and adding one line here — nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from edgeradar.adapters.base import SourceAdapter
from edgeradar.adapters.kalshi import KalshiAdapter
from edgeradar.adapters.manifold import ManifoldAdapter
from edgeradar.adapters.polymarket import PolymarketAdapter
from edgeradar.storage import write_quotes, write_raw

REGISTRY: dict[str, type[SourceAdapter]] = {
    "manifold": ManifoldAdapter,
    "kalshi": KalshiAdapter,
    "polymarket": PolymarketAdapter,
}


@dataclass
class IngestResult:
    """Summary of one source's ingestion run."""

    source: str
    n_raw: int
    n_quotes: int
    raw_path: str | None
    clean_path: str | None


def run_ingest(source: str = "all", *, dry_run: bool = False) -> list[IngestResult]:
    """Run one or all adapters; land raw payloads and normalized quotes.

    Args:
        source: a registered slug (e.g. "manifold") or "all".
        dry_run: if True, adapters read saved sample responses instead of the network.
    """
    if source == "all":
        slugs = list(REGISTRY)
    elif source in REGISTRY:
        slugs = [source]
    else:
        raise ValueError(f"Unknown source {source!r}. Known: {', '.join(REGISTRY)} or 'all'.")

    results: list[IngestResult] = []
    for slug in slugs:
        adapter = REGISTRY[slug](dry_run=dry_run)
        try:
            raw = list(adapter.fetch())
            quotes = list(adapter.normalize(raw))
        except Exception as exc:  # noqa: BLE001
            # In dry-run (tests/CI) a failure is a real bug — let it surface.
            # On a live run, a transient network/API hiccup for one source must
            # not crash the whole pipeline; log it and keep the other sources.
            if dry_run:
                raise
            print(f"[ingest] {slug} failed ({exc}); skipping this source.")
            results.append(
                IngestResult(source=slug, n_raw=0, n_quotes=0, raw_path=None, clean_path=None)
            )
            continue
        raw_path = write_raw(raw)
        clean_path = write_quotes(quotes)
        results.append(
            IngestResult(
                source=slug,
                n_raw=len(raw),
                n_quotes=len(quotes),
                raw_path=str(raw_path) if raw_path else None,
                clean_path=str(clean_path) if clean_path else None,
            )
        )
    return results
