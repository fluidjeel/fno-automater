"""Deterministic helpers shared by Layer 3 strategies.

Everything here is a pure function of its inputs so strategies stay replayable.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trading.domain.contracts import FeatureSnapshot
from trading.strategies.macro import MacroAssessment, MacroBias, accepted_macro_bias

__all__ = [
    "DEFAULT_MACRO_MIN_CONFIDENCE",
    "DEFAULT_TECHNICAL_CONFIDENCE",
    "MIN_TECHNICAL_MOVE_FRACTION",
    "resolve_direction",
    "technical_bias",
]

DEFAULT_MACRO_MIN_CONFIDENCE = Decimal("0.6")
DEFAULT_TECHNICAL_CONFIDENCE = Decimal("0.5")
# Paper-stage hypothesis. This prevents quote noise from becoming a directional
# signal; forward paper evidence must calibrate it before live promotion.
MIN_TECHNICAL_MOVE_FRACTION = Decimal("0.001")


def technical_bias(
    underlying: FeatureSnapshot,
    *,
    min_move_fraction: Decimal = MIN_TECHNICAL_MOVE_FRACTION,
) -> MacroBias:
    """Return a direction only when the close-relative move clears noise."""
    last = underlying.market.last
    close = underlying.market.close
    if last is None or close is None:
        return MacroBias.NEUTRAL
    if close.value <= 0 or min_move_fraction < 0:
        return MacroBias.NEUTRAL
    move = (last.value - close.value) / close.value
    if move >= min_move_fraction:
        return MacroBias.BULLISH
    if move <= -min_move_fraction:
        return MacroBias.BEARISH
    return MacroBias.NEUTRAL


def resolve_direction(
    underlying: FeatureSnapshot,
    macro: MacroAssessment | None,
    now: datetime,
    *,
    min_confidence: Decimal = DEFAULT_MACRO_MIN_CONFIDENCE,
    technical_confidence: Decimal = DEFAULT_TECHNICAL_CONFIDENCE,
) -> tuple[MacroBias, Decimal]:
    """Return ``(bias, confidence)``: a bounded macro read, else the technical read.

    Confidence is the macro's own confidence when the macro drives the decision,
    otherwise a fixed technical-confidence value.
    """
    macro_bias = accepted_macro_bias(macro, now=now, min_confidence=min_confidence)
    if macro_bias is not None and macro is not None:
        return macro_bias, macro.confidence
    return technical_bias(underlying), technical_confidence
