"""Phase-2 upscale envelope (ADESK-D6). Deterministic only; never agent BOUNDED.

PART 7.2: unlock requires declared calibration gates. The agent path remains
capped at size_multiplier <= 1.0 (Phase-1 / AgentDecision Field). Upscale above
1.0 may only come from this deterministic envelope after gates pass.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from trading.domain.contracts.base import ExactDecimal, StrictInt, StrictModel
from trading.domain.enums import ConfidenceBucket

__all__ = [
    "PHASE2_HARD_CAP",
    "PHASE2_MIN_SAMPLE_DEFAULT",
    "PHASE2_M_CEILING_DEFAULT",
    "CalibrationGate",
    "Phase2UpscaleEnvelope",
    "agent_size_multiplier_rejected_above_one",
    "apply_deterministic_upscale",
]

# Spec: suggest 1.25, never above 1.5.
PHASE2_M_CEILING_DEFAULT = Decimal("1.25")
PHASE2_HARD_CAP = Decimal("1.50")
PHASE2_MIN_SAMPLE_DEFAULT = 150


class CalibrationGate(StrictModel):
    """Declared out-of-sample calibration inputs for one role/policy version."""

    scored_decisions: StrictInt = Field(ge=0)
    brier_score: ExactDecimal = Field(ge=Decimal("0"))
    reliability_component: ExactDecimal = Field(ge=Decimal("0"))
    # Empirical accuracy per ConfidenceBucket in ascending bucket order.
    bucket_accuracies: tuple[ExactDecimal, ...]
    min_calibration_sample: StrictInt = Field(ge=1, default=PHASE2_MIN_SAMPLE_DEFAULT)
    max_brier: ExactDecimal = Field(gt=Decimal("0"), default=Decimal("0.25"))
    max_reliability: ExactDecimal = Field(gt=Decimal("0"), default=Decimal("0.05"))

    @model_validator(mode="after")
    def _bucket_count(self) -> Self:
        expected = len(ConfidenceBucket)
        if len(self.bucket_accuracies) != expected:
            raise ValueError(
                f"bucket_accuracies must have one entry per ConfidenceBucket "
                f"({expected}); got {len(self.bucket_accuracies)}"
            )
        for acc in self.bucket_accuracies:
            if acc < 0 or acc > 1:
                raise ValueError("bucket accuracies must be in [0, 1]")
        return self

    @property
    def sample_ok(self) -> bool:
        return self.scored_decisions >= self.min_calibration_sample

    @property
    def brier_ok(self) -> bool:
        return self.brier_score <= self.max_brier

    @property
    def reliability_ok(self) -> bool:
        return self.reliability_component <= self.max_reliability

    @property
    def monotonic_buckets(self) -> bool:
        acc = self.bucket_accuracies
        return all(acc[i] <= acc[i + 1] for i in range(len(acc) - 1))

    @property
    def unlocked(self) -> bool:
        return (
            self.sample_ok
            and self.brier_ok
            and self.reliability_ok
            and self.monotonic_buckets
        )


class Phase2UpscaleEnvelope(StrictModel):
    """Deterministic ceiling after calibration. Agent path must not use this."""

    gate: CalibrationGate
    m_ceiling: ExactDecimal = Field(
        default=PHASE2_M_CEILING_DEFAULT,
        gt=Decimal("1"),
        le=PHASE2_HARD_CAP,
    )

    @model_validator(mode="after")
    def _ceiling_bounds(self) -> Self:
        if self.m_ceiling > PHASE2_HARD_CAP:
            raise ValueError(f"m_ceiling cannot exceed hard cap {PHASE2_HARD_CAP}")
        if self.m_ceiling <= Decimal("1"):
            raise ValueError("Phase-2 m_ceiling must be > 1")
        return self

    @property
    def unlocked(self) -> bool:
        return self.gate.unlocked


def apply_deterministic_upscale(
    base_multiplier: Decimal,
    *,
    envelope: Phase2UpscaleEnvelope,
) -> Decimal:
    """Apply Phase-2 ceiling to a base multiplier when calibration unlocks.

    Returns base_multiplier unchanged when locked. Never called from an agent
    BOUNDED path — callers must be deterministic sizing code.
    """
    if base_multiplier < 0:
        raise ValueError("base_multiplier must be >= 0")
    if not envelope.unlocked:
        # Fail closed: locked envelope cannot upscale; clamp to Phase-1 ceiling.
        return min(base_multiplier, Decimal("1"))
    capped = min(base_multiplier, envelope.m_ceiling)
    if capped > PHASE2_HARD_CAP:
        raise ValueError(f"no code path may exceed hard cap {PHASE2_HARD_CAP}")
    return capped


def agent_size_multiplier_rejected_above_one(multiplier: Decimal) -> bool:
    """Agent path invariant: any proposed m > 1 is rejected (type + policy)."""
    return multiplier > Decimal("1")
