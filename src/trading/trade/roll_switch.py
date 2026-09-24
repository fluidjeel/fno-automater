"""Roll/switch review close path and replacement gating."""

from __future__ import annotations

from datetime import datetime

from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.lifecycle import RollSwitchTransition
from trading.domain.contracts.position import PositionState
from trading.domain.enums import (
    ReasonCode,
    ReviewAction,
    ReviewExecutionStatus,
    ReviewSlotId,
    RollSwitchStatus,
)
from trading.runtime.review_schedule import ReviewSlot, next_review_slot_id
from trading.trade.review import ReviewEvaluation
from trading.trade.review_roll_switch import family_supports_roll_switch

__all__ = [
    "begin_roll_switch_transition",
    "replacement_blocked_reason",
    "resolve_roll_switch_review",
    "transition_after_close",
]


def begin_roll_switch_transition(
    *,
    kind: ReviewAction,
    review_id: str,
    as_of: datetime,
) -> RollSwitchTransition:
    """Open a roll/switch transition when the structure close is submitted."""
    if kind not in {ReviewAction.ROLL, ReviewAction.SWITCH}:
        raise ValueError("roll/switch transition requires ROLL or SWITCH kind")
    return RollSwitchTransition(
        transition_id=f"RST-{review_id}",
        review_id=review_id,
        kind=kind,
        status=RollSwitchStatus.CLOSE_PENDING,
        as_of=as_of,
    )


def transition_after_close(
    transition: RollSwitchTransition, *, as_of: datetime
) -> RollSwitchTransition:
    """Advance a transition once the structure close has fully reconciled."""
    if transition.status is not RollSwitchStatus.CLOSE_PENDING:
        return transition
    return transition.model_copy(
        update={"status": RollSwitchStatus.CLOSE_COMPLETE, "as_of": as_of}
    )


def replacement_blocked_reason(
    transition: RollSwitchTransition | None,
    *,
    entries_blocked: bool,
) -> ReasonCode | None:
    """Return why a replacement cannot proceed, if blocked."""
    if transition is None:
        return ReasonCode.INSTRUMENT_UNKNOWN
    if entries_blocked:
        return ReasonCode.UNKNOWN_ORDER_STATUS
    if transition.status is RollSwitchStatus.CLOSE_PENDING:
        return ReasonCode.UNKNOWN_ORDER_STATUS
    if transition.status is RollSwitchStatus.REPLACEMENT_REJECTED:
        return ReasonCode.CAPITAL_UNAVAILABLE
    if transition.status is RollSwitchStatus.COMPLETE:
        return ReasonCode.OK
    return None


def resolve_roll_switch_review(
    evaluation: ReviewEvaluation,
    *,
    intent: TradeIntent,
    position: PositionState,
    slot: ReviewSlot,
    configured_slots: tuple[ReviewSlot, ...],
) -> tuple[ReviewEvaluation, ReviewExecutionStatus | None, ReviewSlotId | None]:
    """Resolve PROPOSE_ROLL/SWITCH into a G2 structure close without relabeling."""
    next_slot = (
        next_review_slot_id(slot.slot_id, configured_slots)
        if evaluation.action is ReviewAction.HOLD
        and evaluation.reason_code is ReasonCode.OK
        else None
    )
    detail = evaluation.detail
    if next_slot is not None:
        detail = f"{detail}; next review slot {next_slot.value}"

    if evaluation.action is ReviewAction.PROPOSE_ROLL:
        return _resolve_proposal(
            evaluation,
            intent=intent,
            kind=ReviewAction.ROLL,
            detail=detail,
            next_slot=next_slot,
        )

    if evaluation.action is ReviewAction.PROPOSE_SWITCH:
        return _resolve_proposal(
            evaluation,
            intent=intent,
            kind=ReviewAction.SWITCH,
            detail=detail,
            next_slot=next_slot,
        )

    if evaluation.action is ReviewAction.HOLD and next_slot is not None:
        return (
            ReviewEvaluation(
                action=evaluation.action,
                reason_code=evaluation.reason_code,
                detail=detail,
                updated_policy=evaluation.updated_policy,
                exit_quantity_contracts=evaluation.exit_quantity_contracts,
            ),
            ReviewExecutionStatus.NOT_APPLICABLE,
            next_slot,
        )

    status = (
        ReviewExecutionStatus.NOT_APPLICABLE
        if not evaluation.action.is_proposal
        else None
    )
    return evaluation, status, next_slot


def _resolve_proposal(
    evaluation: ReviewEvaluation,
    *,
    intent: TradeIntent,
    kind: ReviewAction,
    detail: str,
    next_slot: ReviewSlotId | None,
) -> tuple[ReviewEvaluation, ReviewExecutionStatus | None, ReviewSlotId | None]:
    if family_supports_roll_switch(intent.family_id):
        return (
            ReviewEvaluation(
                action=evaluation.action,
                reason_code=ReasonCode.OK,
                detail=(
                    f"G2 {kind.value.lower()} close path: {detail}; "
                    "replacement requires fresh Layer 2 approval"
                ),
                updated_policy=evaluation.updated_policy,
                exit_quantity_contracts=None,
                submit_structure_close=True,
                roll_switch_kind=kind,
            ),
            ReviewExecutionStatus.CLOSE_SUBMITTED,
            next_slot,
        )
    return (
        ReviewEvaluation(
            action=evaluation.action,
            reason_code=ReasonCode.PROPOSED_NOT_EXECUTED,
            detail=f"{detail}; family lacks G2 close/open plans",
            updated_policy=evaluation.updated_policy,
        ),
        ReviewExecutionStatus.PROPOSED_NOT_EXECUTED,
        next_slot,
    )
