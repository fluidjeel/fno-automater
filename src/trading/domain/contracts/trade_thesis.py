"""Falsifiable trade thesis and machine-evaluable invalidation conditions.

ADESK-A3: agents may *write* theses; they never evaluate them. Evaluation is
pure deterministic code in trading.analytics.invalidation. Minimums are
enforced here: at least one contradicting reason and two invalidation
conditions. Prose is forbidden in metrics and comparators.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.identification import ConfidenceKind
from trading.domain.enums import (
    Comparator,
    DeskRole,
    DirectionalClaim,
    DriverCode,
    InvalidationMetric,
    InvalidationSeverity,
)

__all__ = ["InvalidationCondition", "TradeThesis"]


class InvalidationCondition(VersionedModel):
    """One machine-checkable falsifier. No free-text predicates."""

    condition_id: NonEmptyStr
    metric: InvalidationMetric
    comparator: Comparator
    threshold: ExactDecimal | NonEmptyStr
    window: NonEmptyStr | None = None
    severity: InvalidationSeverity


class TradeThesis(VersionedModel):
    """Entry-time falsifiable claim. Immutable after hash is set by caller."""

    thesis_id: NonEmptyStr
    trade_id: NonEmptyStr
    snapshot_id: NonEmptyStr
    written_at: UtcDatetime
    author: DeskRole
    model_id: NonEmptyStr
    prompt_version: NonEmptyStr
    directional_claim: DirectionalClaim
    horizon_days: int = Field(ge=1)
    primary_driver: DriverCode
    supporting_reason_codes: tuple[NonEmptyStr, ...] = ()
    contradicting_reason_codes: tuple[NonEmptyStr, ...]
    invalidation: tuple[InvalidationCondition, ...]
    strengthening: tuple[InvalidationCondition, ...] = ()
    confidence: ExactDecimal = Field(ge=Decimal(0), le=Decimal(1))
    confidence_kind: ConfidenceKind
    expected_mfe_r: ExactDecimal | None = None
    expected_mae_r: ExactDecimal | None = None
    thesis_hash: NonEmptyStr

    @model_validator(mode="after")
    def _minimum_falsifiability(self) -> Self:
        if len(self.contradicting_reason_codes) < 1:
            raise ValueError(
                "TradeThesis requires at least one contradicting_reason_code"
            )
        if len(self.invalidation) < 2:
            raise ValueError("TradeThesis requires at least two invalidation conditions")
        return self
