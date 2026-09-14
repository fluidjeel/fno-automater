"""Trade lifecycle and position state management."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from trading.domain.clock import Clock
from trading.domain.contracts.common import ContractRef
from trading.domain.contracts.intent import IntentLeg, TradeIntent
from trading.domain.contracts.order import OrderEvent
from trading.domain.contracts.order_plan import OrderPlan
from trading.domain.contracts.position import (
    ExitPolicy,
    PositionLegState,
    PositionState,
)
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import ExitScope, OrderState, Side, TradeState, Trigger
from trading.domain.ids import IdFactory
from trading.domain.primitives import Price
from trading.domain.state import TRADE_MACHINE, IllegalTransitionError
from trading.risk.reservation import CapitalReservationService
from trading.risk.sizing.credit_spread import is_credit_spread
from trading.risk.sizing.iron_condor import is_iron_condor
from trading.trade.exits import ExitEngine, ExitEvaluation, build_exit_policy

__all__ = [
    "TradeManager",
    "TradeManagerError",
    "UnknownTradeError",
]


class TradeManagerError(Exception):
    """Base error for trade lifecycle failures."""


class UnknownTradeError(TradeManagerError):
    """Raised when a trade id is not tracked."""

    def __init__(self, trade_id: str) -> None:
        self.trade_id = trade_id
        super().__init__(f"unknown trade: {trade_id}")


@dataclass(frozen=True, slots=True)
class _PendingEntry:
    intent: TradeIntent
    plan: OrderPlan


class TradeManager:
    """Track trade state transitions and protective coverage."""

    def __init__(
        self,
        *,
        clock: Clock,
        id_factory: IdFactory,
        reservation_service: CapitalReservationService | None = None,
        exit_engine: ExitEngine | None = None,
    ) -> None:
        self._clock = clock
        self._ids = id_factory
        self._reservations = reservation_service
        self._exit_engine = exit_engine or ExitEngine()
        self._pending: dict[str, _PendingEntry] = {}
        self._positions: dict[str, PositionState] = {}

    def begin_entry(self, intent: TradeIntent, plan: OrderPlan) -> str:
        """Register a trade awaiting entry fills."""
        if not plan.orders:
            raise TradeManagerError("order plan must contain at least one order")
        trade_id = plan.orders[0].identity.trade_id
        self._pending[trade_id] = _PendingEntry(intent=intent, plan=plan)
        return trade_id

    def apply_order_event(
        self,
        event: OrderEvent,
        *,
        intent: TradeIntent | None = None,
        capital_reservation_id: str | None = None,
    ) -> PositionState:
        """Advance trade state from a broker-confirmed order transition."""
        trade_id = event.identity.trade_id
        resolved_intent = intent
        if resolved_intent is None:
            pending = self._pending.get(trade_id)
            if pending is None and trade_id not in self._positions:
                raise UnknownTradeError(trade_id)
            if pending is not None:
                resolved_intent = pending.intent
        if resolved_intent is None:
            raise UnknownTradeError(trade_id)

        now = self._clock.now_utc()
        position = self._positions.get(trade_id)

        if event.state is OrderState.PARTIAL:
            return self._record_partial_entry(
                trade_id,
                event,
                resolved_intent,
                position=position,
                now=now,
            )

        if event.state is OrderState.FILLED:
            opened = self._record_full_entry(
                trade_id,
                event,
                resolved_intent,
                position=position,
                now=now,
            )
            self._finalize_entry_if_complete(
                trade_id,
                opened,
                capital_reservation_id=capital_reservation_id,
            )
            return opened

        if event.state in {
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        }:
            if position is None:
                raise UnknownTradeError(trade_id)
            self._pending.pop(trade_id, None)
            return self._transition(
                position,
                TradeState.CLOSED,
                trigger=Trigger.BROKER_EVENT,
                now=now,
            )

        if event.state in {OrderState.SUBMITTING, OrderState.ACKNOWLEDGED}:
            if position is None:
                position = self._create_opening(trade_id, resolved_intent, now=now)
                self._positions[trade_id] = position
            elif position.state is TradeState.PENDING_ENTRY:
                return self._transition(
                    position,
                    TradeState.OPENING,
                    trigger=Trigger.LOCAL_COMMAND,
                    now=now,
                )
        return position if position is not None else self._require(trade_id)

    def register_protective_orders(
        self,
        trade_id: str,
        protective_order_ids: tuple[str, ...],
    ) -> PositionState:
        """Attach protective coverage and open the trade. Invariant 16."""
        position = self._require(trade_id)
        if not protective_order_ids:
            raise TradeManagerError(
                "protective coverage requires at least one order id"
            )
        now = self._clock.now_utc()
        updated = position.model_copy(
            update={
                "protective_order_ids": protective_order_ids,
                "as_of": now,
            }
        )
        if updated.state is TradeState.OPENING:
            return self._transition(
                updated,
                TradeState.OPEN,
                trigger=Trigger.BROKER_EVENT,
                now=now,
                opened_at=now,
            )
        if updated.state in {TradeState.REPAIR_REQUIRED, TradeState.OPEN}:
            self._positions[trade_id] = updated
            return updated
        raise TradeManagerError(
            f"cannot register protection while trade is {updated.state}"
        )

    def evaluate_exit(
        self,
        trade_id: str,
        feature: FeatureSnapshot,
        intent: TradeIntent,
        *,
        leg_snapshots: Mapping[str, FeatureSnapshot] | None = None,
    ) -> ExitEvaluation:
        """Run deterministic exit rules for one open trade."""
        position = self._require(trade_id)
        return self._exit_engine.evaluate(
            position,
            feature,
            intent,
            now=self._clock.now_utc(),
            leg_snapshots=leg_snapshots,
        )

    def apply_exit_order_event(
        self,
        event: OrderEvent,
        *,
        capital_reservation_id: str | None = None,
    ) -> PositionState:
        """Advance an exit leg from broker confirmation to CLOSED."""
        trade_id = event.identity.trade_id
        position = self._require(trade_id)
        now = self._clock.now_utc()
        if event.state in {OrderState.SUBMITTING, OrderState.ACKNOWLEDGED}:
            if position.state is TradeState.EXIT_PENDING:
                return self._transition(
                    position,
                    TradeState.CLOSING,
                    trigger=Trigger.LOCAL_COMMAND,
                    now=now,
                )
            return position
        if event.state is OrderState.FILLED:
            closing = position
            if closing.state is TradeState.EXIT_PENDING:
                closing = self._transition(
                    closing,
                    TradeState.CLOSING,
                    trigger=Trigger.LOCAL_COMMAND,
                    now=now,
                )
            remaining_legs = _legs_after_exit_fill(closing, event)
            if not remaining_legs:
                closed = self._transition(
                    closing,
                    TradeState.CLOSED,
                    trigger=Trigger.BROKER_EVENT,
                    now=now,
                )
                if (
                    capital_reservation_id is not None
                    and self._reservations is not None
                ):
                    self._reservations.release(
                        capital_reservation_id,
                        trigger=Trigger.BROKER_EVENT,
                    )
                self._positions[trade_id] = closed
                return closed
            updated = closing.model_copy(update={"legs": remaining_legs, "as_of": now})
            self._positions[trade_id] = updated
            return updated
        if (
            event.state
            in {OrderState.REJECTED, OrderState.CANCELLED, OrderState.EXPIRED}
            and position.state is TradeState.CLOSING
        ):
            reopened = self._transition(
                position,
                TradeState.OPEN,
                trigger=Trigger.RECONCILIATION,
                now=now,
            )
            self._positions[trade_id] = reopened
            return reopened
        return position

    def apply_exit_evaluation(
        self,
        trade_id: str,
        evaluation: ExitEvaluation,
    ) -> PositionState:
        """Persist policy tightening and request exit when required."""
        position = self._require(trade_id)
        now = self._clock.now_utc()
        policy = evaluation.updated_policy or position.exit_policy
        updated = position.model_copy(
            update={"exit_policy": policy, "as_of": now},
        )
        if evaluation.should_exit and updated.state is TradeState.OPEN:
            return self._transition(
                updated,
                TradeState.EXIT_PENDING,
                trigger=Trigger.LOCAL_COMMAND,
                now=now,
            )
        self._positions[trade_id] = updated
        return updated

    def get_position(self, trade_id: str) -> PositionState | None:
        return self._positions.get(trade_id)

    def is_pending(self, trade_id: str) -> bool:
        return trade_id in self._pending

    def list_positions(self) -> tuple[PositionState, ...]:
        return tuple(self._positions.values())

    def _finalize_entry_if_complete(
        self,
        trade_id: str,
        position: PositionState,
        *,
        capital_reservation_id: str | None,
    ) -> None:
        pending = self._pending.get(trade_id)
        if pending is None or not _entry_complete(position, pending.plan):
            return
        if capital_reservation_id is not None and self._reservations is not None:
            self._reservations.commit(capital_reservation_id)
        self._pending.pop(trade_id, None)

    def _create_opening(
        self,
        trade_id: str,
        intent: TradeIntent,
        *,
        now: datetime,
    ) -> PositionState:
        pending = self._pending.get(trade_id)
        entry_price = _planned_entry_price(pending, intent)
        monitor_leg = _monitor_leg(intent)
        return PositionState(
            trade_id=trade_id,
            intent_id=intent.intent_id,
            strategy_id=intent.strategy_id,
            state=TradeState.OPENING,
            legs=(
                PositionLegState(
                    leg_id=monitor_leg.leg_id,
                    contract=monitor_leg.contract,
                    side=monitor_leg.side,
                    quantity_contracts=1,
                    average_entry_price=entry_price,
                ),
            ),
            exit_policy=_build_trade_exit_policy(
                intent,
                trade_id=trade_id,
                policy_id=self._ids.new_id("EXIT-POL"),
                entry_price=entry_price,
                initialized_at=now,
            ),
            protective_order_ids=(),
            as_of=now,
        )

    def _record_partial_entry(
        self,
        trade_id: str,
        event: OrderEvent,
        intent: TradeIntent,
        *,
        position: PositionState | None,
        now: datetime,
    ) -> PositionState:
        """Partial entry fill routes to REPAIR_REQUIRED. Invariant 15."""
        leg = _intent_leg_for_event(intent, event)
        fill_price = _fill_price(event)
        legs = _upsert_leg(
            position.legs if position is not None else (),
            leg_id=leg.leg_id,
            contract=leg.contract,
            side=leg.side,
            quantity_contracts=event.filled_quantity,
            average_entry_price=fill_price,
        )
        entry_price = _planned_entry_price(self._pending.get(trade_id), intent)
        policy = _build_trade_exit_policy(
            intent,
            trade_id=trade_id,
            policy_id=self._ids.new_id("EXIT-POL"),
            entry_price=entry_price,
            initialized_at=now,
        )
        base = position or self._create_opening(trade_id, intent, now=now)
        updated = base.model_copy(
            update={
                "legs": legs,
                "exit_policy": policy,
                "protective_order_ids": ("REPAIR-PENDING",),
                "as_of": now,
            }
        )
        if updated.state is not TradeState.REPAIR_REQUIRED:
            updated = self._transition(
                updated,
                TradeState.REPAIR_REQUIRED,
                trigger=Trigger.BROKER_EVENT,
                now=now,
            )
        self._pending.pop(trade_id, None)
        return updated

    def _record_full_entry(
        self,
        trade_id: str,
        event: OrderEvent,
        intent: TradeIntent,
        *,
        position: PositionState | None,
        now: datetime,
    ) -> PositionState:
        leg = _intent_leg_for_event(intent, event)
        fill_price = _fill_price(event)
        policy_id = (
            position.exit_policy.policy_id
            if position is not None
            else self._ids.new_id("EXIT-POL")
        )
        entry_price = _planned_entry_price(self._pending.get(trade_id), intent)
        policy = _build_trade_exit_policy(
            intent,
            trade_id=trade_id,
            policy_id=policy_id,
            entry_price=entry_price,
            initialized_at=now,
            quantity_contracts=event.filled_quantity,
        )
        existing_legs = position.legs if position is not None else ()
        legs = _upsert_leg(
            existing_legs,
            leg_id=leg.leg_id,
            contract=leg.contract,
            side=leg.side,
            quantity_contracts=event.filled_quantity,
            average_entry_price=fill_price,
            current_stop_price=policy.stop_price,
        )
        base = position or self._create_opening(trade_id, intent, now=now)
        updated = base.model_copy(
            update={
                "legs": legs,
                "exit_policy": policy,
                "as_of": now,
            }
        )
        if updated.state is TradeState.PENDING_ENTRY:
            updated = self._transition(
                updated,
                TradeState.OPENING,
                trigger=Trigger.BROKER_EVENT,
                now=now,
            )
        self._positions[trade_id] = updated
        return updated

    def _transition(
        self,
        position: PositionState,
        target: TradeState,
        *,
        trigger: Trigger,
        now: datetime,
        opened_at: datetime | None = None,
    ) -> PositionState:
        try:
            TRADE_MACHINE.transition(
                position.state,
                target,
                trigger=trigger,
                at=now,
            )
        except IllegalTransitionError as exc:
            raise TradeManagerError(exc.record.detail) from exc
        updates: dict[str, object] = {"state": target, "as_of": now}
        if opened_at is not None:
            updates["opened_at"] = opened_at
        updated = position.model_copy(update=updates)
        self._positions[position.trade_id] = updated
        return updated

    def _require(self, trade_id: str) -> PositionState:
        position = self._positions.get(trade_id)
        if position is None:
            raise UnknownTradeError(trade_id)
        return position


def _planned_entry_price(
    pending: _PendingEntry | None,
    intent: TradeIntent,
) -> Price:
    if pending is None:
        raise TradeManagerError("missing pending entry for trade")
    monitor_leg = _monitor_leg(intent)
    for order in pending.plan.orders:
        if order.leg_id == monitor_leg.leg_id:
            limit_price = order.command.limit_price
            if limit_price is None:
                raise TradeManagerError("planned entry order is missing a limit price")
            return limit_price
    raise TradeManagerError(
        f"planned entry order missing for monitor leg {monitor_leg.leg_id}"
    )


def _monitor_leg(intent: TradeIntent) -> IntentLeg:
    """Return the leg whose price drives exit monitoring for this structure."""
    if is_credit_spread(intent):
        return next(leg for leg in intent.legs if leg.side is Side.SELL)
    buy_legs = [leg for leg in intent.legs if leg.side is Side.BUY]
    if buy_legs:
        return buy_legs[0]
    return intent.legs[0]


def _exit_scope(intent: TradeIntent) -> ExitScope:
    if is_iron_condor(intent):
        return ExitScope.STRATEGY_PNL
    return ExitScope.LEG_PRICE


def _build_trade_exit_policy(
    intent: TradeIntent,
    *,
    trade_id: str,
    policy_id: str,
    entry_price: Price,
    initialized_at: datetime,
    quantity_contracts: int = 1,
) -> ExitPolicy:
    scope = _exit_scope(intent)
    monitor_leg = _monitor_leg(intent)
    return build_exit_policy(
        intent.exit_template,
        trade_id=trade_id,
        policy_id=policy_id,
        entry_price=entry_price,
        initialized_at=initialized_at,
        scope=scope,
        quantity_contracts=quantity_contracts,
        monitor_side=monitor_leg.side,
    )


def _intent_leg_for_event(intent: TradeIntent, event: OrderEvent) -> IntentLeg:
    symbol = event.command.contract.symbol
    for leg in intent.legs:
        if leg.contract.symbol == symbol:
            return leg
    raise TradeManagerError(f"no intent leg matches contract {symbol}")


def _upsert_leg(
    legs: tuple[PositionLegState, ...],
    *,
    leg_id: str,
    contract: ContractRef,
    side: Side,
    quantity_contracts: int,
    average_entry_price: Price,
    current_stop_price: Price | None = None,
) -> tuple[PositionLegState, ...]:
    updated = PositionLegState(
        leg_id=leg_id,
        contract=contract,
        side=side,
        quantity_contracts=quantity_contracts,
        average_entry_price=average_entry_price,
        current_stop_price=current_stop_price,
    )
    kept = tuple(leg for leg in legs if leg.leg_id != leg_id)
    return (*kept, updated)


def _fill_price(event: OrderEvent) -> Price:
    if event.average_fill_price is None:
        raise TradeManagerError("entry fill is missing an average fill price")
    return event.average_fill_price


def _entry_complete(position: PositionState, plan: OrderPlan) -> bool:
    filled_by_leg = {leg.leg_id: leg for leg in position.legs}
    for order in plan.orders:
        filled = filled_by_leg.get(order.leg_id)
        if filled is None:
            return False
        if filled.quantity_contracts != order.command.quantity_contracts:
            return False
    return True


def _legs_after_exit_fill(
    position: PositionState,
    event: OrderEvent,
) -> tuple[PositionLegState, ...]:
    symbol = event.command.contract.symbol
    remaining: list[PositionLegState] = []
    for leg in position.legs:
        if leg.contract.symbol != symbol:
            remaining.append(leg)
            continue
        left = leg.quantity_contracts - event.filled_quantity
        if left > 0:
            remaining.append(leg.model_copy(update={"quantity_contracts": left}))
    return tuple(remaining)
