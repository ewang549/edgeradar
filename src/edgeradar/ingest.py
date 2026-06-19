"""Ingestion runner: drive adapters and land raw + clean Parquet.

The adapter REGISTRY maps a source slug to its adapter class. Adding a new source
later means importing its class and adding one line here — nothing else changes.
"""

from __future__ import annotations

from dataclasses import dataclass

from edgeradar.adapters.base import SourceAdapter
from edgeradar.adapters.kalshi import KalshiAdapter
from edgeradar.adapters.manifold import ManifoldAdapter
from edgeradar.adapters.oddsapi import OddsApiAdapter
from edgeradar.adapters.polymarket import PolymarketAdapter
from edgeradar.storage import write_quotes, write_raw
from edgeradar.targeting import resolve_categories

REGISTRY: dict[str, type[SourceAdapter]] = {
    "manifold": ManifoldAdapter,
    "kalshi": KalshiAdapter,
    "polymarket": PolymarketAdapter,
    "oddsapi": OddsApiAdapter,
}


@dataclass
class IngestResult:
    """Summary of one source's ingestion run."""

    source: str
    n_raw: int
    n_quotes: int
    raw_path: str | None
    clean_path: str | None


def _build_adapter(slug: str, *, dry_run: bool, categories: list[str] | None) -> SourceAdapter:
    """Construct a registered adapter, wiring category targeting where supported.

    `categories=None` (the default) means "pull everything" — unchanged behavior.
    Named categories (see edgeradar.targeting) narrow *which real markets* each
    adapter asks its venue for; they never fabricate a market.
    """
    if not categories:
        return REGISTRY[slug](dry_run=dry_run)
    t = resolve_categories(categories)
    if slug == "kalshi" and t.kalshi_series:
        return KalshiAdapter(dry_run=dry_run, series_tickers=t.kalshi_series)
    if slug == "manifold" and t.keywords:
        return ManifoldAdapter(dry_run=dry_run, keywords=t.keywords)
    if slug == "polymarket" and t.keywords:
        return PolymarketAdapter(dry_run=dry_run, keywords=t.keywords)
    if slug == "oddsapi" and t.oddsapi_sports:
        return OddsApiAdapter(dry_run=dry_run, sports=",".join(t.oddsapi_sports))
    return REGISTRY[slug](dry_run=dry_run)


def run_ingest(
    source: str = "all", *, dry_run: bool = False, categories: list[str] | None = None
) -> list[IngestResult]:
    """Run one or all adapters; land raw payloads and normalized quotes.

    Args:
        source: a registered slug (e.g. "manifold") or "all".
        dry_run: if True, adapters read saved sample responses instead of the network.
        categories: optional named categories (see edgeradar.targeting) to focus
            ingestion on real markets likely to overlap across platforms (e.g.
            ["world_cup", "crypto", "elections"]). Default (None) pulls everything.
    """
    if source == "all":
        slugs = list(REGISTRY)
    elif source in REGISTRY:
        slugs = [source]
    else:
        raise ValueError(f"Unknown source {source!r}. Known: {', '.join(REGISTRY)} or 'all'.")

    results: list[IngestResult] = []
    for slug in slugs:
        adapter = _build_adapter(slug, dry_run=dry_run, categories=categories)
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
