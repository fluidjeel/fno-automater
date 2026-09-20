"""Bias-battery report contract (ADESK-A6 / AGENT_DESK_SPEC PART 9.3).

Deterministic metrics only. Agents narrate; they never compute these numbers.
"""

from __future__ import annotations

from enum import StrEnum, unique

from pydantic import Field

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)

__all__ = [
    "BiasMetricId",
    "BiasMetricResult",
    "BiasReport",
]


@unique
class BiasMetricId(StrEnum):
    """Closed set of PART 9.3 bias metrics (exactly 11)."""

    DISPOSITION_EFFECT = "DISPOSITION_EFFECT"
    PREMATURE_EXIT = "PREMATURE_EXIT"
    RECENCY = "RECENCY"
    REVENGE = "REVENGE"
    OVERCONFIDENCE = "OVERCONFIDENCE"
    CONFIRMATION = "CONFIRMATION"
    ANCHORING = "ANCHORING"
    FORM_STREAK = "FORM_STREAK"
    HINDSIGHT_DRIFT = "HINDSIGHT_DRIFT"
    SELECTION_DRIFT = "SELECTION_DRIFT"
    COST_BLINDNESS = "COST_BLINDNESS"


class BiasMetricResult(StrictModel):
    """One computed metric; value is None when under-sampled."""

    metric_id: BiasMetricId
    value: ExactDecimal | None = None
    sample_size: StrictInt = Field(ge=0)
    band_breached: StrictBool = False
    detail: NonEmptyStr | None = None


class BiasReport(VersionedModel):
    """Weekly bias battery over a decision/trade cohort."""

    as_of: UtcDatetime
    cohort_id: NonEmptyStr
    metrics: tuple[BiasMetricResult, ...]
    attention_required: StrictBool

    def result_for(self, metric_id: BiasMetricId) -> BiasMetricResult | None:
        for row in self.metrics:
            if row.metric_id is metric_id:
                return row
        return None
