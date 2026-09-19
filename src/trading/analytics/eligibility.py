"""Fail-closed promotion eligibility. Never writes live configuration."""

from __future__ import annotations

import hashlib
from datetime import datetime

from trading.config.evaluation import EligibilityThresholds
from trading.domain.contracts.evaluation import (
    CohortScorecard,
    PromotionEligibilityResult,
)
from trading.domain.enums import EligibilityStatus

__all__ = ["evaluate_eligibility"]


def evaluate_eligibility(
    scorecard: CohortScorecard,
    thresholds: EligibilityThresholds,
    *,
    evaluated_at: datetime,
    threshold_checksum: str,
) -> PromotionEligibilityResult:
    """Return eligibility. Win rate and gross P&L alone never pass."""
    sample_gates: list[str] = []
    hard_gates: list[str] = []

    if scorecard.observation_days < thresholds.min_observation_days:
        sample_gates.append("min_observation_days")
    if scorecard.signal_count < thresholds.min_signals:
        sample_gates.append("min_signals")
    if scorecard.closed_trade_count < thresholds.min_closed_trades:
        sample_gates.append("min_closed_trades")

    if thresholds.require_confirmed_costs and not scorecard.costs_confirmed:
        hard_gates.append("require_confirmed_costs")
    if scorecard.expectancy is None or (
        scorecard.expectancy < thresholds.min_expectancy()
    ):
        hard_gates.append("min_expectancy")
    if thresholds.require_positive_expectancy_lower_95 and (
        scorecard.expectancy_lower_95 is None
        or scorecard.expectancy_lower_95.amount <= 0
    ):
        hard_gates.append("positive_expectancy_lower_95")
    if (
        scorecard.profit_factor is None
        or scorecard.profit_factor < thresholds.min_profit_factor
    ):
        hard_gates.append("min_profit_factor")
    if scorecard.max_drawdown > thresholds.max_drawdown():
        hard_gates.append("max_drawdown")
    if scorecard.consecutive_losses > thresholds.max_consecutive_losses:
        hard_gates.append("max_consecutive_losses")
    if scorecard.fill_rate < thresholds.min_fill_rate:
        hard_gates.append("min_fill_rate")
    if scorecard.reject_rate > thresholds.max_reject_rate:
        hard_gates.append("max_reject_rate")
    if (
        scorecard.average_entry_slippage is not None
        and scorecard.average_entry_slippage > thresholds.max_average_slippage()
    ):
        hard_gates.append("max_average_slippage")
    if (
        scorecard.max_single_trade_pnl_share is not None
        and scorecard.max_single_trade_pnl_share > thresholds.max_single_trade_pnl_share
    ):
        hard_gates.append("max_single_trade_pnl_share")
    if scorecard.regime_count < thresholds.min_regime_count:
        hard_gates.append("min_regime_count")
    if thresholds.require_daily_reconciliation and scorecard.unreconciled_count > 0:
        hard_gates.append("require_daily_reconciliation")
    if thresholds.require_no_p0_p1 and scorecard.incident_p0_p1_count > 0:
        hard_gates.append("require_no_p0_p1")

    if sample_gates:
        status = EligibilityStatus.INSUFFICIENT_SAMPLE
        failed = tuple(sample_gates)
    elif hard_gates:
        status = EligibilityStatus.INELIGIBLE
        failed = tuple(hard_gates)
    else:
        status = EligibilityStatus.ELIGIBLE
        failed = ()

    result_id = hashlib.sha256(
        f"{scorecard.scorecard_id}|{threshold_checksum}|{evaluated_at.isoformat()}".encode()
    ).hexdigest()[:16]
    return PromotionEligibilityResult(
        result_id=result_id,
        experiment_id=scorecard.experiment_id,
        scorecard_id=scorecard.scorecard_id,
        status=status,
        evaluated_at=evaluated_at,
        failed_gates=failed,
        threshold_checksum=threshold_checksum,
        detail="",
    )
