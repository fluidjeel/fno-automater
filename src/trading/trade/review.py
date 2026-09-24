"""Deterministic twice-daily positional review. Frozen policy only.

Reviews supplement continuous stop evaluation. They never widen a stop, never
rewrite the entry-time exit template, and never auto-submit a hedge or roll.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from trading.ai.terminal_enforcement import (
    TerminalEnforcementOutcome,
    enforce_terminal_run_conditions,
)
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.lifecycle import PositionReviewRecord
from trading.domain.contracts.position import ExitPolicy, PositionState
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import (
    ExitScope,
    HoldingStyle,
    InvalidationMetric,
    ReasonCode,
    ReviewAction,
    ReviewSlotId,
    Side,
    TradeState,
)
from trading.trade.exits import ExitEngine, monitor_leg, tighten_exit_policy

_MIN_PARTIAL_CONTRACTS = 2


@dataclass(frozen=True, slots=True)
class ReviewEvaluation:
    """Outcome of one scheduled review against a frozen exit policy."""

    action: ReviewAction
    reason_code: ReasonCode
    detail: str
    updated_policy: ExitPolicy | None = None
    exit_quantity_contracts: int | None = None

    @property
    def should_submit_exit(self) -> bool:
        return self.action.submits_exit


class ReviewEngine:
    """Decide HOLD / TIGHTEN / PARTIAL / FULL / propose HEDGE or ROLL."""

    def __init__(self, exit_engine: ExitEngine | None = None) -> None:
        self._exits = exit_engine or ExitEngine()

    def evaluate(  # noqa: PLR0911, PLR0912 - explicit frozen-policy action table
        self,
        position: PositionState,
        intent: TradeIntent,
        feature: FeatureSnapshot,
        *,
        now: datetime,
        slot_id: ReviewSlotId,
        session_date: date,
        holding_style: HoldingStyle,
        prior_reviews: tuple[PositionReviewRecord, ...] = (),
        leg_snapshots: Mapping[str, FeatureSnapshot] | None = None,
        run_condition_observations: (
            Mapping[InvalidationMetric, Decimal | str] | None
        ) = None,
    ) -> ReviewEvaluation:
        """Return one review action under the frozen entry-time policy."""
        if holding_style is not HoldingStyle.POSITIONAL:
            return ReviewEvaluation(
                action=ReviewAction.HOLD,
                reason_code=ReasonCode.OK,
                detail="intraday trades are not in the positional review book",
            )
        if position.state in {TradeState.EXIT_PENDING, TradeState.CLOSING}:
            return ReviewEvaluation(
                action=ReviewAction.HOLD,
                reason_code=ReasonCode.OK,
                detail="exit already pending; review does not replace in-flight orders",
            )
        if position.state is not TradeState.OPEN:
            return ReviewEvaluation(
                action=ReviewAction.HOLD,
                reason_code=ReasonCode.OK,
                detail=f"no review action while trade is {position.state.value}",
            )
        if any(
            item.slot_id is slot_id and item.session_date == session_date
            for item in prior_reviews
        ):
            return ReviewEvaluation(
                action=ReviewAction.HOLD,
                reason_code=ReasonCode.REVIEW_DUPLICATE_SLOT,
                detail="this slot is already recorded for the trade",
            )
        if feature.quality.state.blocks_new_exposure:
            return ReviewEvaluation(
                action=ReviewAction.HOLD,
                reason_code=ReasonCode.PRICE_UNAVAILABLE,
                detail="stale or invalid quotes skip this review; position stays open",
            )

        if run_condition_observations is not None:
            dte_pre = _days_to_expiry(feature)
            enforced = enforce_terminal_run_conditions(
                position.exit_policy,
                observations=run_condition_observations,
                days_to_expiry=dte_pre,
            )
            if enforced.requires_exit:
                return ReviewEvaluation(
                    action=ReviewAction.FULL_EXIT,
                    reason_code=ReasonCode.OK,
                    detail=enforced.detail,
                    updated_policy=enforced.updated_policy,
                    exit_quantity_contracts=_open_quantity(position),
                )
            if enforced.outcome is TerminalEnforcementOutcome.REVERTED_TO_FLATTEN:
                # Apply revert then continue remaining review against flatten policy.
                position = position.model_copy(
                    update={"exit_policy": enforced.updated_policy}
                )
        exit_eval = self._exits.evaluate(
            position,
            feature,
            intent,
            now=now,
            leg_snapshots=leg_snapshots,
        )
        if exit_eval.should_exit:
            return ReviewEvaluation(
                action=ReviewAction.FULL_EXIT,
                reason_code=ReasonCode.OK,
                detail=f"frozen policy exit: {exit_eval.detail}",
                updated_policy=exit_eval.updated_policy,
                exit_quantity_contracts=_open_quantity(position),
            )

        dte = _days_to_expiry(feature)
        expiry_days = position.exit_policy.exit_before_expiry_days
        if dte is not None and expiry_days is not None and dte <= expiry_days:
            return ReviewEvaluation(
                action=ReviewAction.FULL_EXIT,
                reason_code=ReasonCode.CONTRACT_EXPIRED,
                detail=(
                    f"days to expiry {dte} is at or inside frozen "
                    f"exit_before_expiry_days {expiry_days}"
                ),
                exit_quantity_contracts=_open_quantity(position),
            )

        if not _already_partialed(prior_reviews):
            partial_qty = _partial_exit_quantity(position, position.exit_policy)
            if partial_qty is not None:
                return ReviewEvaluation(
                    action=ReviewAction.PARTIAL_EXIT,
                    reason_code=ReasonCode.OK,
                    detail=(
                        "scale out after frozen break-even or trail is active; "
                        f"exit {partial_qty} contracts, remainder stays protected"
                    ),
                    exit_quantity_contracts=partial_qty,
                )

        tightened = _proposed_tighten(position, intent, feature)
        if tightened is not None:
            if not _stop_is_tighter(
                position.exit_policy, tightened, monitor_leg(intent).side
            ):
                return ReviewEvaluation(
                    action=ReviewAction.HOLD,
                    reason_code=ReasonCode.STOP_WIDEN_REJECTED,
                    detail="proposed stop is not strictly tighter; frozen stop kept",
                )
            return ReviewEvaluation(
                action=ReviewAction.TIGHTEN_STOP,
                reason_code=ReasonCode.OK,
                detail="frozen trail or break-even rule tightens the software stop",
                updated_policy=tightened,
            )

        if dte is not None and expiry_days is None and dte <= 1:
            return ReviewEvaluation(
                action=ReviewAction.PROPOSE_ROLL,
                reason_code=ReasonCode.REVIEW_PROPOSAL_REQUIRES_L2,
                detail=(
                    "near expiry with no frozen flatten; ROLL is a new trade and "
                    "requires Layer 2 approval (auto-submit blocked)"
                ),
            )

        if _past_stop_midpoint(position, intent, feature):
            return ReviewEvaluation(
                action=ReviewAction.PROPOSE_HEDGE,
                reason_code=ReasonCode.REVIEW_PROPOSAL_REQUIRES_L2,
                detail=(
                    "adverse excursion past the midpoint to the frozen stop; "
                    "HEDGE is a new trade and requires Layer 2 (auto-submit blocked)"
                ),
            )
        return ReviewEvaluation(
            action=ReviewAction.HOLD,
            reason_code=ReasonCode.OK,
            detail="frozen policy unchanged; positional review holds",
        )


def assert_stop_not_wider(
    previous: ExitPolicy, candidate: ExitPolicy, side: Side
) -> None:
    """Invariant 17: a review must not widen worst-case vs the frozen stop."""
    if not _stop_is_tighter(previous, candidate, side) and not _same_stop(
        previous, candidate
    ):
        raise ValueError("review stop is wider than the frozen policy (invariant 17)")


def _already_partialed(prior: tuple[PositionReviewRecord, ...]) -> bool:
    return any(item.action is ReviewAction.PARTIAL_EXIT for item in prior)


def _open_quantity(position: PositionState) -> int:
    return sum(leg.quantity_contracts for leg in position.legs)


def _partial_exit_quantity(position: PositionState, policy: ExitPolicy) -> int | None:
    if len(position.legs) != 1:
        return None
    if not (policy.breakeven_active or policy.trailing_active):
        return None
    qty = position.legs[0].quantity_contracts
    if qty < _MIN_PARTIAL_CONTRACTS:
        return None
    scale = qty // _MIN_PARTIAL_CONTRACTS
    if scale < 1 or qty - scale < 1:
        return None
    return scale


def _proposed_tighten(
    position: PositionState, intent: TradeIntent, feature: FeatureSnapshot
) -> ExitPolicy | None:
    if position.exit_policy.scope is not ExitScope.LEG_PRICE:
        return None
    watched = monitor_leg(intent)
    leg = next((item for item in position.legs if item.leg_id == watched.leg_id), None)
    if leg is None:
        return None
    monitor = feature.market.bid if watched.side is Side.BUY else feature.market.ask
    if monitor is None:
        return None
    return tighten_exit_policy(
        position.exit_policy,
        intent.exit_template,
        entry_price=leg.average_entry_price,
        monitor_price=monitor,
    )


def _pnl_stop_is_tighter(previous: ExitPolicy, candidate: ExitPolicy) -> bool:
    if candidate.pnl_stop is not None and previous.pnl_stop is not None:
        if candidate.pnl_stop.amount > previous.pnl_stop.amount:
            return True
        if candidate.pnl_stop.amount < previous.pnl_stop.amount:
            return False
        return (
            candidate.current_stop_distance_ticks < previous.current_stop_distance_ticks
        )
    if previous.pnl_stop is None and candidate.pnl_stop is not None:
        return True
    if candidate.pnl_stop is None and previous.pnl_stop is not None:
        return False
    return candidate.current_stop_distance_ticks < previous.current_stop_distance_ticks


def _leg_price_stop_is_tighter(
    previous: ExitPolicy, candidate: ExitPolicy, side: Side
) -> bool:
    if previous.stop_price is None:
        return candidate.stop_price is not None
    if candidate.stop_price is None:
        return False
    if side is Side.BUY:
        return candidate.stop_price.value > previous.stop_price.value
    return candidate.stop_price.value < previous.stop_price.value


def _stop_is_tighter(previous: ExitPolicy, candidate: ExitPolicy, side: Side) -> bool:
    if candidate.current_stop_distance_ticks > previous.current_stop_distance_ticks:
        return False
    if previous.scope in {
        ExitScope.STRATEGY_PNL,
        ExitScope.SPREAD_VALUE,
    } or candidate.scope in {ExitScope.STRATEGY_PNL, ExitScope.SPREAD_VALUE}:
        return _pnl_stop_is_tighter(previous, candidate)
    return _leg_price_stop_is_tighter(previous, candidate, side)


def _same_stop(previous: ExitPolicy, candidate: ExitPolicy) -> bool:
    return (
        previous.stop_price == candidate.stop_price
        and (
            previous.current_stop_distance_ticks
            == candidate.current_stop_distance_ticks
        )
        and previous.pnl_stop == candidate.pnl_stop
    )


def _days_to_expiry(feature: FeatureSnapshot) -> int | None:
    if feature.derivatives is None:
        return None
    return feature.derivatives.days_to_expiry


def _past_stop_midpoint(
    position: PositionState, intent: TradeIntent, feature: FeatureSnapshot
) -> bool:
    watched = monitor_leg(intent)
    leg = next((item for item in position.legs if item.leg_id == watched.leg_id), None)
    stop = position.exit_policy.stop_price
    monitor = feature.market.bid if watched.side is Side.BUY else feature.market.ask
    if leg is None or stop is None or monitor is None:
        return False
    entry = leg.average_entry_price.value
    risk = entry - stop.value if watched.side is Side.BUY else stop.value - entry
    if risk <= 0:
        return False
    adverse = (
        entry - monitor.value if watched.side is Side.BUY else monitor.value - entry
    )
    if adverse <= 0:
        return False
    midpoint = risk / Decimal(2)
    still_above_stop = (
        monitor.value > stop.value
        if watched.side is Side.BUY
        else monitor.value < stop.value
    )
    return adverse >= midpoint and still_above_stop
