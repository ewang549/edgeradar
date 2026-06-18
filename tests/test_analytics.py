"""Tests for the explainable analytics layer (confidence, uncertainty, reliability)."""

from __future__ import annotations

import pytest

from edgeradar.analytics import (
    DISPERSION_LOOSE,
    confidence_tier,
    decompose_edge,
    match_quality,
    score_source_reliability,
    uncertainty_band,
)

# --------------------------------------------------------------------------- #
# Edge decomposition
# --------------------------------------------------------------------------- #


def test_edge_decomposes_into_shrinking_layers():
    b = decompose_edge(implied_prob=0.60, consensus=0.50, trade_cost=0.03, dispersion=0.04)
    assert b.raw_edge == pytest.approx(0.10)
    assert b.fee_adjusted_edge == pytest.approx(0.07)
    assert b.uncertainty_adj_edge == pytest.approx(0.03)
    assert b.survives_costs
    assert b.survives_uncertainty


def test_edge_can_die_at_each_layer():
    # Survives fees but not the noise of a disagreeing consensus.
    b = decompose_edge(0.55, 0.50, trade_cost=0.01, dispersion=0.08)
    assert b.fee_adjusted_edge == pytest.approx(0.04)
    assert b.uncertainty_adj_edge == pytest.approx(-0.04)
    assert b.survives_costs
    assert not b.survives_uncertainty


def test_negative_inputs_are_floored():
    b = decompose_edge(0.50, 0.50, trade_cost=-1.0, dispersion=-1.0)
    assert b.trade_cost == 0.0
    assert b.dispersion == 0.0
    assert b.raw_edge == 0.0


# --------------------------------------------------------------------------- #
# Confidence tiers (must mirror mart_divergence.sql)
# --------------------------------------------------------------------------- #


def test_confidence_high_needs_three_sources_and_tight_agreement():
    c = confidence_tier(n_sources=3, dispersion=0.02)
    assert c.tier == "high"
    assert c.reasons


def test_confidence_medium_for_two_sources_moderate_spread():
    assert confidence_tier(n_sources=2, dispersion=0.08).tier == "medium"


def test_confidence_low_when_single_source_or_noisy():
    assert confidence_tier(n_sources=1, dispersion=0.0).tier == "low"
    assert confidence_tier(n_sources=4, dispersion=DISPERSION_LOOSE + 0.01).tier == "low"


def test_uncertainty_band_brackets_the_edge():
    lo, hi = uncertainty_band(fee_adjusted_edge=0.05, dispersion=0.03)
    assert lo == pytest.approx(0.02)
    assert hi == pytest.approx(0.08)


# --------------------------------------------------------------------------- #
# Source reliability
# --------------------------------------------------------------------------- #


def test_fresh_complete_calibrated_source_grades_high():
    r = score_source_reliability("kalshi", age_minutes=2, completeness=1.0, brier=0.05)
    assert r.score >= 90
    assert r.grade == "A"


def test_stale_incomplete_source_grades_low():
    r = score_source_reliability("manifold", age_minutes=3000, completeness=0.5, brier=None)
    assert r.score < 60
    assert r.grade == "F"


def test_missing_calibration_redistributes_weight_not_penalizes():
    # Same freshness+completeness; one with calibration, one without. The version
    # without calibration should not be dragged down toward zero.
    with_calib = score_source_reliability("a", age_minutes=2, completeness=1.0, brier=0.0)
    without = score_source_reliability("b", age_minutes=2, completeness=1.0, brier=None)
    assert without.calibration_score is None
    assert without.score == pytest.approx(100.0, abs=0.1)
    assert with_calib.score == pytest.approx(100.0, abs=0.1)


def test_reliability_inputs_clamped():
    r = score_source_reliability("x", age_minutes=None, completeness=2.0, brier=None)
    assert 0.0 <= r.score <= 100.0


# --------------------------------------------------------------------------- #
# Match quality
# --------------------------------------------------------------------------- #


def test_override_is_always_strong():
    assert match_quality(0.1, is_override=True).tier == "strong"


def test_match_quality_tiers_by_confidence():
    assert match_quality(0.9).tier == "strong"
    assert match_quality(0.7).tier == "probable"
    assert match_quality(0.3).tier == "weak"
