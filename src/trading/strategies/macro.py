"""Bounded macro context for Layer 3.

Layer 4 generates a macro assessment asynchronously. Layer 3 never calls the
live news/macro agent; it only consumes a frozen ``MacroAssessment`` fixture
through a deterministic acceptance policy. Missing, stale or low-confidence
assessments are ignored, never fabricated.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum, unique

from pydantic import Field

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictModel,
    UtcDatetime,
)

__all__ = ["MacroAssessment", "MacroBias", "accepted_macro_bias"]


@unique
class MacroBias(StrEnum):
    """Bounded directional read on the market, produced off the decision path."""

    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class MacroAssessment(StrictModel):
    """A frozen, evidence-bearing macro read. Not a live agent."""

    regime: NonEmptyStr
    directional_bias: MacroBias
    confidence: ExactDecimal = Field(ge=Decimal(0), le=Decimal(1))
    fresh_until: UtcDatetime
    evidence_ids: tuple[NonEmptyStr, ...] = ()
    model_version: NonEmptyStr


def accepted_macro_bias(
    assessment: MacroAssessment | None,
    *,
    now: datetime,
    min_confidence: Decimal,
) -> MacroBias | None:
    """Deterministic policy: only a fresh, confident, non-neutral read is used.

    Returns ``None`` when the assessment is absent, stale, low-confidence or
    neutral — the caller falls back to its technical rule.
    """
    if assessment is None:
        return None
    if now >= assessment.fresh_until:
        return None
    if assessment.confidence < min_confidence:
        return None
    if assessment.directional_bias is MacroBias.NEUTRAL:
        return None
    return assessment.directional_bias
