"""Polymarket adapter tests (data-only consensus source)."""

from __future__ import annotations

import pytest

from edgeradar.adapters.polymarket import PolymarketAdapter


def test_polymarket_normalizes_binary_and_filters_closed():
    quotes = PolymarketAdapter(dry_run=True).run()
    # 3 active binary markets; 1 closed market is filtered out.
    assert len(quotes) == 3
    by_id = {q.market_id: q for q in quotes}
    # SpaceX "Yes" price 0.9755 -> implied prob.
    assert by_id["1971078"].implied_prob == pytest.approx(0.9755)
    assert all(q.source == "polymarket" and q.outcome == "YES" for q in quotes)
    assert all(0.0 < q.implied_prob < 1.0 for q in quotes)
    # Polymarket is data-only: no trading cost is attributed to it.
    assert all(q.trade_cost == 0.0 for q in quotes)
