"""Phase 0 smoke tests.

Confirm the scaffold imports, settings load, the normalization math is correct,
and the adapter interface enforces its contract. Each later phase adds its own
tests next to its code.
"""

from __future__ import annotations

import math

import pytest

from edgeradar.adapters.base import SourceAdapter
from edgeradar.config import get_settings
from edgeradar.normalize import (
    american_to_prob,
    decimal_to_prob,
    kalshi_cents_to_prob,
    remove_vig_two_way,
)


def test_settings_load_and_execution_disabled() -> None:
    s = get_settings()
    assert s.minio_bucket  # has a default
    assert s.enable_order_execution is False  # hard guardrail


def test_american_odds_conversion() -> None:
    assert math.isclose(american_to_prob(150), 0.4)
    assert math.isclose(american_to_prob(-200), 2 / 3, rel_tol=1e-9)


def test_decimal_and_kalshi_conversions() -> None:
    assert math.isclose(decimal_to_prob(2.0), 0.5)
    assert math.isclose(kalshi_cents_to_prob(37), 0.37)


def test_remove_vig_sums_to_one() -> None:
    a, b = remove_vig_two_way(0.55, 0.55)
    assert math.isclose(a + b, 1.0)
    assert math.isclose(a, 0.5)


def test_adapter_requires_source_slug() -> None:
    class Bad(SourceAdapter):
        def fetch(self):  # type: ignore[override]
            return []

        def normalize(self, raw):  # type: ignore[override]
            return []

    with pytest.raises(NotImplementedError):
        Bad()  # no `source` set -> must refuse to construct
