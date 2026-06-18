"""Explainable analytics layer: confidence, uncertainty, and reliability scoring.

Everything here is a *pure function* of numbers EdgeRadar already measures — no
trained model, no hidden state, no network. That is deliberate: a divergence
dashboard is only trustworthy if every label it shows can be explained in one
sentence to a skeptical reader. Each scorer therefore returns not just a number
or tier but the human-readable reasons behind it.

Three ideas live here:

1. Edge decomposition. A raw price gap is not an opportunity. We peel it back in
   layers so the dashboard can show exactly where an apparent edge goes:
       raw_edge                = |implied_prob - consensus|
       fee_adjusted_edge       = raw_edge - trade_cost
       uncertainty_adj_edge    = fee_adjusted_edge - dispersion
   The last layer charges the edge for how much the platforms disagree with each
   other: a 5-point gap means little when the "consensus" is itself spread over
   10 points.

2. Confidence tiers. How much to trust a single divergence depends on how many
   independent platforms priced the event and how tightly they agree. More
   sources + tighter agreement = higher confidence. Mirrors the SQL in
   mart_divergence so the dashboard and the warehouse never disagree.

3. Source reliability. A 0–100 score per data source from three observable
   signals — freshness, completeness, and (where we have settled outcomes)
   calibration. Used to weight sources and to flag a stale or thin feed before a
   reader leans on it.

All functions clamp their inputs and degrade gracefully on missing data, because
on live feeds something is always missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Tunable thresholds. Kept here, named, and documented so the logic is auditable
# rather than buried as magic numbers inside conditionals.
# --------------------------------------------------------------------------- #

# Confidence: cross-platform price dispersion (stddev, in probability units).
DISPERSION_TIGHT = 0.05  # platforms agree to within ~5 points
DISPERSION_LOOSE = 0.10  # beyond this, "consensus" is too noisy to lean on

# Source reliability: a feed older than this (minutes) scores zero on freshness.
FRESHNESS_FULL_MINUTES = 15.0  # fresher than this = full marks
FRESHNESS_ZERO_MINUTES = 24 * 60.0  # a day stale = no freshness credit

# Calibration: Brier score range we map onto a 0–1 sub-score. 0.0 is perfect;
# 0.25 is the score of always guessing 50%, our "no skill" anchor.
BRIER_PERFECT = 0.0
BRIER_NO_SKILL = 0.25

# Weights for the blended reliability score (must sum to 1.0).
RELIABILITY_WEIGHTS = {"freshness": 0.4, "completeness": 0.4, "calibration": 0.2}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp x into [lo, hi]; treat NaN as the low bound."""
    if x != x:  # NaN
        return lo
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- #
# 1. Edge decomposition
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EdgeBreakdown:
    """The three honest layers of an apparent edge, all in probability units."""

    raw_edge: float  # |implied_prob - consensus|
    fee_adjusted_edge: float  # raw_edge - trade_cost
    uncertainty_adj_edge: float  # fee_adjusted_edge - dispersion
    trade_cost: float
    dispersion: float

    @property
    def survives_costs(self) -> bool:
        return self.fee_adjusted_edge > 0

    @property
    def survives_uncertainty(self) -> bool:
        return self.uncertainty_adj_edge > 0


def decompose_edge(
    implied_prob: float,
    consensus: float,
    trade_cost: float = 0.0,
    dispersion: float = 0.0,
) -> EdgeBreakdown:
    """Peel an apparent price gap into raw / fee-adjusted / uncertainty-adjusted edge.

    `dispersion` is the stddev of platform prices for the event (how much the
    sources disagree). All values are in probability units (0–1).
    """
    raw = abs(implied_prob - consensus)
    fee_adj = raw - max(trade_cost, 0.0)
    unc_adj = fee_adj - max(dispersion, 0.0)
    return EdgeBreakdown(
        raw_edge=raw,
        fee_adjusted_edge=fee_adj,
        uncertainty_adj_edge=unc_adj,
        trade_cost=max(trade_cost, 0.0),
        dispersion=max(dispersion, 0.0),
    )


# --------------------------------------------------------------------------- #
# 2. Confidence tiers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Confidence:
    """A confidence tier for a single divergence, with the reasons behind it."""

    tier: str  # "high" | "medium" | "low"
    reasons: list[str] = field(default_factory=list)

    @property
    def emoji(self) -> str:
        return {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(self.tier, "⚪")


def confidence_tier(n_sources: int, dispersion: float) -> Confidence:
    """Rate trust in a divergence from platform count and price agreement.

    Mirrors the CASE expression in mart_divergence.sql exactly, so the dashboard
    and the warehouse always show the same tier for the same row.
    """
    d = max(dispersion, 0.0)
    if n_sources >= 3 and d < DISPERSION_TIGHT:
        return Confidence(
            "high",
            [
                f"{n_sources} independent platforms priced this event",
                f"they agree tightly (dispersion {d:.3f} < {DISPERSION_TIGHT})",
            ],
        )
    if n_sources >= 2 and d < DISPERSION_LOOSE:
        return Confidence(
            "medium",
            [
                f"{n_sources} platforms priced this event",
                f"moderate agreement (dispersion {d:.3f} < {DISPERSION_LOOSE})",
            ],
        )
    reasons: list[str] = []
    if n_sources < 2:
        reasons.append("only one platform priced this event (no real consensus)")
    if d >= DISPERSION_LOOSE:
        reasons.append(f"platforms disagree a lot (dispersion {d:.3f} ≥ {DISPERSION_LOOSE})")
    return Confidence("low", reasons or ["not enough corroboration"])


def uncertainty_band(fee_adjusted_edge: float, dispersion: float) -> tuple[float, float]:
    """A plain ± interval around the fee-adjusted edge.

    We have no posterior to integrate, so we use the directly observed
    cross-platform dispersion as the half-width — an honest, legible proxy for
    "how much could this move if the consensus is itself noisy".
    """
    half = max(dispersion, 0.0)
    return (fee_adjusted_edge - half, fee_adjusted_edge + half)


# --------------------------------------------------------------------------- #
# 3. Source reliability
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceReliability:
    """A 0–100 reliability score for one data source, with component sub-scores."""

    source: str
    score: float  # 0–100 blended
    grade: str  # A / B / C / D / F
    freshness_score: float  # 0–1
    completeness_score: float  # 0–1
    calibration_score: float | None  # 0–1, or None when no settled outcomes yet
    reasons: list[str] = field(default_factory=list)


def _freshness_score(age_minutes: float | None) -> float:
    """1.0 when fresh, decaying linearly to 0.0 at FRESHNESS_ZERO_MINUTES."""
    if age_minutes is None:
        return 0.0
    if age_minutes <= FRESHNESS_FULL_MINUTES:
        return 1.0
    span = FRESHNESS_ZERO_MINUTES - FRESHNESS_FULL_MINUTES
    return _clamp(1.0 - (age_minutes - FRESHNESS_FULL_MINUTES) / span)


def _calibration_score(brier: float | None) -> float | None:
    """Map a Brier score onto 0–1 (1.0 = perfect, 0.0 = no-skill 0.25 or worse)."""
    if brier is None:
        return None
    span = BRIER_NO_SKILL - BRIER_PERFECT
    return _clamp(1.0 - (brier - BRIER_PERFECT) / span)


def _grade(score_100: float) -> str:
    for cutoff, letter in ((90, "A"), (80, "B"), (70, "C"), (60, "D")):
        if score_100 >= cutoff:
            return letter
    return "F"


def score_source_reliability(
    source: str,
    age_minutes: float | None,
    completeness: float | None,
    brier: float | None = None,
) -> SourceReliability:
    """Blend freshness, completeness, and calibration into a 0–100 reliability score.

    - age_minutes: minutes since the source's most recent snapshot (None = unknown).
    - completeness: fraction of expected fields present, 0–1 (1 - null_rate).
    - brier: Brier score from settled outcomes, if available (lower is better).

    When calibration is unavailable, its weight is redistributed across the two
    observable components so a brand-new source isn't penalized for lack of history.
    """
    fresh = _freshness_score(age_minutes)
    complete = _clamp(completeness if completeness is not None else 0.0)
    calib = _calibration_score(brier)

    w = dict(RELIABILITY_WEIGHTS)
    if calib is None:
        # Redistribute calibration weight proportionally onto the other two.
        spare = w.pop("calibration")
        total = w["freshness"] + w["completeness"]
        w["freshness"] += spare * w["freshness"] / total
        w["completeness"] += spare * w["completeness"] / total
        blended = w["freshness"] * fresh + w["completeness"] * complete
    else:
        blended = w["freshness"] * fresh + w["completeness"] * complete + w["calibration"] * calib

    score_100 = round(blended * 100, 1)
    reasons: list[str] = []
    if fresh < 0.5:
        reasons.append("data is stale" if age_minutes else "freshness unknown")
    if complete < 0.9:
        reasons.append(f"{(1 - complete) * 100:.0f}% of fields missing/null")
    if calib is None:
        reasons.append("no settled outcomes yet — calibration not scored")
    elif calib < 0.5:
        reasons.append("poorly calibrated on settled markets")
    if not reasons:
        reasons.append("fresh, complete, and well-behaved")

    return SourceReliability(
        source=source,
        score=score_100,
        grade=_grade(score_100),
        freshness_score=round(fresh, 3),
        completeness_score=round(complete, 3),
        calibration_score=round(calib, 3) if calib is not None else None,
        reasons=reasons,
    )


# --------------------------------------------------------------------------- #
# 4. Event match quality
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class MatchQuality:
    """How much to trust that two markets are really the same event."""

    tier: str  # "strong" | "probable" | "weak"
    reasons: list[str] = field(default_factory=list)


def match_quality(confidence: float, *, is_override: bool = False) -> MatchQuality:
    """Turn an entity-resolution confidence in [0,1] into a readable quality tier.

    `is_override` marks a human-confirmed match, which is always strong regardless
    of the fuzzy score.
    """
    if is_override:
        return MatchQuality("strong", ["human-confirmed via the override table"])
    c = _clamp(confidence)
    if c >= 0.80:
        return MatchQuality("strong", [f"high title/date similarity ({c:.2f})"])
    if c >= 0.60:
        return MatchQuality(
            "probable",
            [f"moderate similarity ({c:.2f}) — worth a glance before trusting"],
        )
    return MatchQuality("weak", [f"low similarity ({c:.2f}) — likely not the same event"])
