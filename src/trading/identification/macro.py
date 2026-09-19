"""Deterministic publisher from curated macro-news factors to Layer 3 context."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from trading.data.macro_news import MacroNewsFactor
from trading.strategies.macro import MacroAssessment, MacroBias

__all__ = ["publish_macro_assessment"]

_DIRECTION_THRESHOLD = Decimal("0.20")
_TTL = timedelta(hours=1)


def publish_macro_assessment(
    factor: MacroNewsFactor | None,
) -> MacroAssessment | None:
    """Freeze a bounded assessment; missing evidence remains missing."""
    if factor is None or not factor.evidence_ids:
        return None
    if factor.sentiment >= _DIRECTION_THRESHOLD:
        bias = MacroBias.BULLISH
    elif factor.sentiment <= -_DIRECTION_THRESHOLD:
        bias = MacroBias.BEARISH
    else:
        bias = MacroBias.NEUTRAL
    confidence = min(Decimal(1), factor.coverage)
    return MacroAssessment(
        regime="CURATED_MACRO_NEWS",
        directional_bias=bias,
        confidence=confidence,
        fresh_until=factor.calculated_at + _TTL,
        evidence_ids=factor.evidence_ids,
        model_version=factor.calculation_version,
    )
