"""The Odds API adapter tests (sportsbook consensus, data-only)."""

from __future__ import annotations

import pytest

from edgeradar.adapters.oddsapi import OddsApiAdapter


def test_oddsapi_vig_removed_consensus():
    quotes = OddsApiAdapter(dry_run=True).run()
    # 2 events x 2 outcomes = 4 quotes.
    assert len(quotes) == 4
    assert all(q.source == "oddsapi" and q.outcome == "YES" for q in quotes)
    # Vig removed: the two outcomes of each event sum to ~1.0.
    nba = [q for q in quotes if "Celtics" in q.title or "Lakers" in q.title]
    assert sum(q.implied_prob for q in nba) == pytest.approx(1.0, abs=1e-6)
    # Celtics are heavy favorites (~0.88 after de-vig).
    celtics = next(q for q in nba if "Celtics to win" in q.title)
    assert 0.85 <= celtics.implied_prob <= 0.92
    # Data-only source: no trading cost attributed.
    assert all(q.trade_cost == 0.0 for q in quotes)
