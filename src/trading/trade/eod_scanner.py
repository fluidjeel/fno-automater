"""EOD scanner logic for evaluating carry-forward decisions.

Pure functions computing stop loss and carry-forward action based on
end-of-day conviction re-assessment. No wall clock reads; ``as_of`` is
injected.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from trading.universe.contracts import (
    CarryForwardAction,
    CarryForwardDecision,
    ConvictionAssessment,
)

__all__ = ["compute_positional_stop", "evaluate_carry_forward"]


def evaluate_carry_forward(
    *,
    symbol: str,
    current_pnl: Decimal,
    conviction_at_close: ConvictionAssessment,
    regime_aligned: bool,
    min_conviction_for_carry: Decimal,
    as_of: datetime,
    decision_id: str,
) -> CarryForwardDecision:
    """Decide whether to carry forward or close a position at EOD.

    Carry forward requires ALL three conditions:
    1. Conviction score >= threshold (e.g. 65)
    2. Position is in profit (current_pnl > 0)
    3. Market regime still aligned with trade direction
    """
    in_profit = current_pnl > Decimal("0")
    conviction_high = conviction_at_close.conviction_score >= min_conviction_for_carry

    if conviction_high and in_profit and regime_aligned:
        return CarryForwardDecision(
            decision_id=decision_id,
            symbol=symbol,
            action=CarryForwardAction.CARRY_FORWARD,
            current_pnl=current_pnl,
            conviction_at_close=conviction_at_close,
            regime_aligned=regime_aligned,
            in_profit=in_profit,
            decided_at=as_of,
            reason="Conviction high, position in profit, and regime aligned",
        )

    reasons: list[str] = []
    if not conviction_high:
        reasons.append(
            f"conviction {conviction_at_close.conviction_score} "
            f"< {min_conviction_for_carry}"
        )
    if not in_profit:
        reasons.append(f"position not in profit (PnL={current_pnl})")
    if not regime_aligned:
        reasons.append("regime no longer aligned with trade direction")

    return CarryForwardDecision(
        decision_id=decision_id,
        symbol=symbol,
        action=CarryForwardAction.CLOSE,
        current_pnl=current_pnl,
        conviction_at_close=conviction_at_close,
        regime_aligned=regime_aligned,
        in_profit=in_profit,
        decided_at=as_of,
        reason=f"Close: {'; '.join(reasons)}",
    )


def compute_positional_stop(
    *,
    entry_price: Decimal,
    current_price: Decimal,
    direction: str,
    atr: Decimal,
    previous_day_high: Decimal,
    previous_day_low: Decimal,
) -> Decimal:
    """Compute a widened stop for positional carry-forward.

    For LONG: stop = max(previous_day_low, entry_price - 2*ATR)
    For SHORT: stop = min(previous_day_high, entry_price + 2*ATR)

    This gives the position room to breathe overnight while still
    protecting against gap-down/gap-up scenarios.
    """
    two_atr = Decimal("2") * atr
    if direction == "LONG":
        return max(previous_day_low, entry_price - two_atr)
    if direction == "SHORT":
        return min(previous_day_high, entry_price + two_atr)
    raise ValueError(f"Unknown direction: {direction!r}; expected 'LONG' or 'SHORT'")
