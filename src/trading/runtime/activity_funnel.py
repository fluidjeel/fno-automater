"""Build the four-mode activity funnel from one supervised paper cycle."""

from __future__ import annotations

from typing import TYPE_CHECKING

from trading.domain.contracts.cycle_evidence import (
    ActivityFunnelSummary,
    FunnelDrop,
)
from trading.domain.enums import FunnelStage, ReasonCode, RiskAction

if TYPE_CHECKING:
    from trading.runtime.paper_runner import PaperCycleResult

__all__ = ["build_activity_funnel"]


def build_activity_funnel(result: PaperCycleResult) -> ActivityFunnelSummary:
    """Emit evaluation → exit funnel drops for one cycle."""
    drops: list[FunnelDrop] = []
    approved = 0
    suppressed = 0

    if result.entries_blocked:
        drops.append(
            FunnelDrop(
                stage=FunnelStage.EVALUATION,
                strategy_id="portfolio",
                detail="entries blocked for this cycle",
                reason_codes=(ReasonCode.ENTRY_FROZEN,),
            )
        )

    for outcome in result.outcomes:
        mode_id = None
        family_id = None
        if outcome.intents:
            mode_id = outcome.intents[0].mode_id
            family_id = outcome.intents[0].family_id
        for reason in outcome.entry_blocked_reasons:
            drops.append(
                FunnelDrop(
                    stage=FunnelStage.MODE_RISK,
                    strategy_id=outcome.strategy_id,
                    mode_id=mode_id,
                    family_id=family_id,
                    reason_codes=(reason,),
                    detail="entry blocked before gateway",
                )
            )
        if not outcome.executed and not outcome.intents:
            stage = FunnelStage.ELIGIBLE_SIGNAL
            if outcome.rejection_reasons:
                stage = FunnelStage.BOUND_CONTRACTS
            drops.append(
                FunnelDrop(
                    stage=stage,
                    strategy_id=outcome.strategy_id,
                    mode_id=mode_id,
                    family_id=family_id,
                    reason_codes=outcome.rejection_reasons,
                    detail="strategy abstained or produced no intent",
                )
            )
        for decision in outcome.decisions:
            if decision.action is RiskAction.REJECT:
                drops.append(
                    FunnelDrop(
                        stage=FunnelStage.MODE_RISK,
                        strategy_id=outcome.strategy_id,
                        mode_id=mode_id,
                        family_id=family_id,
                        reason_codes=decision.reason_codes,
                        detail="risk gateway rejected intent",
                    )
                )
        if outcome.order_events:
            approved += 1
        elif outcome.intents and all(
            decision.action is not RiskAction.REJECT for decision in outcome.decisions
        ):
            drops.append(
                FunnelDrop(
                    stage=FunnelStage.ORDER,
                    strategy_id=outcome.strategy_id,
                    mode_id=mode_id,
                    family_id=family_id,
                    reason_codes=(),
                    detail="intent approved but no order event recorded",
                )
            )

    if result.arbitration_result is not None:
        for item in result.arbitration_result.suppressed_intents:
            suppressed += 1
            family = (
                item.candidate_family_id.value
                if item.candidate_family_id is not None
                else "unknown"
            )
            drops.append(
                FunnelDrop(
                    stage=FunnelStage.PORTFOLIO,
                    strategy_id=family,
                    mode_id=item.candidate_mode_id,
                    family_id=family,
                    reason_codes=(item.reason_code,),
                    detail=item.detail,
                )
            )
        approved += len(result.arbitration_result.approved_intents)

    return ActivityFunnelSummary(
        drops=tuple(drops),
        approved_count=approved,
        suppressed_count=suppressed,
    )
