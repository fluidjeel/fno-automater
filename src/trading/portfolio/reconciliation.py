"""Compare local durable state against broker truth.

Invariant 5: broker-reported positions and orders are external truth.
Invariant 6: inconsistent state blocks new entries.
Invariant 9: startup begins in RECOVERY; entries wait for reconciliation.
Invariant 25: every comparison produces an auditable ReconciliationEvent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading.broker.ports import BrokerPort
from trading.domain.clock import Clock
from trading.domain.contracts.common import Versions
from trading.domain.contracts.order import OrderEvent
from trading.domain.contracts.portfolio import (
    PendingOrderSummary,
    PortfolioSnapshot,
    PortfolioView,
    PositionRecord,
)
from trading.domain.contracts.reconciliation import ReconciliationEvent
from trading.domain.contracts.reconciliation_result import ReconciliationResult
from trading.domain.enums import (
    DifferenceClass,
    OrderState,
    ReasonCode,
    ReconciliationTrigger,
    Severity,
    SystemState,
    Trigger,
)
from trading.domain.ids import IdFactory
from trading.domain.state import SYSTEM_MACHINE
from trading.portfolio.snapshot import (
    build_broker_snapshot,
    sum_active_reservations,
)
from trading.portfolio.view import build_portfolio_view
from trading.storage.trading_store import TradingEventType, TradingStore

__all__ = ["PortfolioReconciler", "ReconcileOutcome"]


@dataclass(frozen=True, slots=True)
class LocalPortfolioState:
    """Operational mirror rebuilt from the durable event log."""

    orders: dict[str, OrderEvent]
    reserved_capital_ref: str


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    """One reconciliation run and the broker snapshot it produced."""

    result: ReconciliationResult
    broker_snapshot: PortfolioSnapshot
    portfolio_view: PortfolioView


class PortfolioReconciler:
    """Boot and periodic reconciliation against broker truth."""

    def __init__(
        self,
        broker: BrokerPort,
        store: TradingStore,
        *,
        clock: Clock,
        id_factory: IdFactory,
        versions: Versions,
    ) -> None:
        self._broker = broker
        self._store = store
        self._clock = clock
        self._ids = id_factory
        self._versions = versions

    def boot_reconcile(self, account_id: str) -> ReconcileOutcome:
        """Run boot reconciliation, persist evidence and update readiness."""
        started_at = self._clock.now_utc()
        prior_state, _ = self._store.get_system_state()
        reconcile_from = prior_state
        if prior_state is SystemState.STARTING:
            SYSTEM_MACHINE.transition(
                SystemState.STARTING,
                SystemState.RECOVERY,
                trigger=Trigger.STARTUP,
                at=started_at,
            )
            self._store.set_system_state(SystemState.RECOVERY, updated_at=started_at)
            reconcile_from = SystemState.RECOVERY

        funds = self._broker.get_funds()
        reserved_capital = sum_active_reservations(
            self._store.list_reservations(),
            currency=funds.equity.currency,
        )
        local = rebuild_local_state(self._store, reserved_capital_ref="local-event-log")
        broker_positions = self._broker.get_positions()
        broker_pending = self._broker.get_pending_orders()
        events = _compare_local_to_broker(
            local=local,
            broker_positions=broker_positions,
            broker_pending=broker_pending,
            account_id=account_id,
            trigger=ReconciliationTrigger.BOOT,
            detected_at=started_at,
            id_factory=self._ids,
        )
        entries_blocked = any(
            event.entries_blocked and not event.is_resolved for event in events
        )
        resulting_state = _resulting_system_state(reconcile_from, entries_blocked)
        if resulting_state is not reconcile_from:
            SYSTEM_MACHINE.transition(
                reconcile_from,
                resulting_state,
                trigger=Trigger.RECONCILIATION,
                at=started_at,
            )

        completed_at = self._clock.now_utc()
        result_id = self._ids.new_id("RECON")
        broker_snapshot = build_broker_snapshot(
            self._broker,
            account_id=account_id,
            versions=self._versions,
            id_factory=self._ids,
            reserved_capital=reserved_capital,
            reconciliation_ref=result_id,
        )
        result = ReconciliationResult(
            result_id=result_id,
            trigger=ReconciliationTrigger.BOOT,
            events=tuple(events),
            entries_blocked=entries_blocked,
            prior_system_state=prior_state,
            resulting_system_state=resulting_state,
            broker_snapshot_ref=broker_snapshot.portfolio_snapshot_id,
            local_snapshot_ref=local.reserved_capital_ref,
            started_at=started_at,
            completed_at=completed_at,
        )
        self._persist(result)
        portfolio_view = build_portfolio_view(
            broker_snapshot,
            system_state=resulting_state,
            entries_blocked=entries_blocked,
        )
        return ReconcileOutcome(
            result=result,
            broker_snapshot=broker_snapshot,
            portfolio_view=portfolio_view,
        )

    def _persist(self, result: ReconciliationResult) -> None:
        for event in result.events:
            self._store.append(
                TradingEventType.RECONCILIATION_EVENT,
                event,
                event_id=event.event_id,
            )
        self._store.set_system_state(
            result.resulting_system_state,
            last_reconciliation_ref=result.result_id,
            updated_at=result.completed_at,
        )


def rebuild_local_state(
    store: TradingStore,
    *,
    reserved_capital_ref: str,
) -> LocalPortfolioState:
    """Replay order events to rebuild the latest local order mirror."""
    orders: dict[str, OrderEvent] = {}
    for stored in store.read_events():
        if stored.event_type is not TradingEventType.ORDER_EVENT:
            continue
        event = stored.deserialize()
        if not isinstance(event, OrderEvent):
            raise TypeError("ORDER_EVENT payload must deserialize to OrderEvent")
        orders[event.identity.internal_order_id] = event
    return LocalPortfolioState(
        orders=orders,
        reserved_capital_ref=reserved_capital_ref,
    )


def _resulting_system_state(
    reconcile_from: SystemState,
    entries_blocked: bool,
) -> SystemState:
    if entries_blocked:
        return reconcile_from
    if reconcile_from in {SystemState.RECOVERY, SystemState.DEGRADED}:
        return SystemState.READY
    return reconcile_from


def _compare_local_to_broker(
    *,
    local: LocalPortfolioState,
    broker_positions: tuple[PositionRecord, ...],
    broker_pending: tuple[PendingOrderSummary, ...],
    account_id: str,
    trigger: ReconciliationTrigger,
    detected_at: datetime,
    id_factory: IdFactory,
) -> list[ReconciliationEvent]:
    events: list[ReconciliationEvent] = []
    events.extend(
        _compare_positions(
            local=local,
            broker_positions=broker_positions,
            account_id=account_id,
            trigger=trigger,
            detected_at=detected_at,
            id_factory=id_factory,
        )
    )
    events.extend(
        _compare_orders(
            local=local,
            broker_pending=broker_pending,
            account_id=account_id,
            trigger=trigger,
            detected_at=detected_at,
            id_factory=id_factory,
        )
    )
    if not events:
        events.append(
            _matched_event(
                scope=f"account/{account_id}/portfolio",
                trigger=trigger,
                detected_at=detected_at,
                id_factory=id_factory,
            )
        )
    return events


def _compare_positions(
    *,
    local: LocalPortfolioState,
    broker_positions: tuple[PositionRecord, ...],
    account_id: str,
    trigger: ReconciliationTrigger,
    detected_at: datetime,
    id_factory: IdFactory,
) -> list[ReconciliationEvent]:
    scope = f"account/{account_id}/positions"
    local_trade_ids = {
        event.identity.trade_id
        for event in local.orders.values()
        if event.state is OrderState.FILLED
    }
    broker_by_trade = {position.trade_id: position for position in broker_positions}
    events: list[ReconciliationEvent] = []

    for trade_id in sorted(local_trade_ids - broker_by_trade.keys()):
        events.append(
            ReconciliationEvent(
                event_id=id_factory.new_id("REC"),
                scope=scope,
                trigger=trigger,
                expected_local_ref=trade_id,
                difference_class=DifferenceClass.LOCAL_ORDER_ABSENT_AT_BROKER,
                severity=Severity.CRITICAL,
                reason_code=ReasonCode.RECONCILIATION_UNRESOLVED,
                repair_action="query broker and freeze affected scope",
                entries_blocked=True,
                detected_at=detected_at,
            )
        )

    for trade_id in sorted(broker_by_trade.keys() - local_trade_ids):
        events.append(
            ReconciliationEvent(
                event_id=id_factory.new_id("REC"),
                scope=scope,
                trigger=trigger,
                observed_broker_ref=trade_id,
                difference_class=DifferenceClass.UNEXPECTED_BROKER_STATE,
                severity=Severity.CRITICAL,
                reason_code=ReasonCode.RECONCILIATION_UNRESOLVED,
                repair_action="import broker position into local mirror",
                entries_blocked=True,
                detected_at=detected_at,
            )
        )

    if not events:
        events.append(
            _matched_event(
                scope=scope,
                trigger=trigger,
                detected_at=detected_at,
                id_factory=id_factory,
            )
        )
    return events


def _compare_orders(
    *,
    local: LocalPortfolioState,
    broker_pending: tuple[PendingOrderSummary, ...],
    account_id: str,
    trigger: ReconciliationTrigger,
    detected_at: datetime,
    id_factory: IdFactory,
) -> list[ReconciliationEvent]:
    scope = f"account/{account_id}/orders"
    local_working = {
        order_id: event
        for order_id, event in local.orders.items()
        if event.state.is_working or event.state is OrderState.UNKNOWN
    }
    broker_pending_by_id = {
        pending.internal_order_id: pending for pending in broker_pending
    }
    events: list[ReconciliationEvent] = []

    for order_id in sorted(local_working.keys() - broker_pending_by_id.keys()):
        events.append(
            ReconciliationEvent(
                event_id=id_factory.new_id("REC"),
                scope=scope,
                trigger=trigger,
                expected_local_ref=order_id,
                difference_class=DifferenceClass.LOCAL_ORDER_ABSENT_AT_BROKER,
                severity=Severity.CRITICAL,
                reason_code=ReasonCode.RECONCILIATION_UNRESOLVED,
                repair_action="query broker and freeze affected scope",
                entries_blocked=True,
                detected_at=detected_at,
            )
        )

    for order_id in sorted(broker_pending_by_id.keys() - local_working.keys()):
        events.append(
            ReconciliationEvent(
                event_id=id_factory.new_id("REC"),
                scope=scope,
                trigger=trigger,
                observed_broker_ref=order_id,
                difference_class=DifferenceClass.UNEXPECTED_BROKER_STATE,
                severity=Severity.CRITICAL,
                reason_code=ReasonCode.RECONCILIATION_UNRESOLVED,
                repair_action="import broker order into local mirror",
                entries_blocked=True,
                detected_at=detected_at,
            )
        )

    if not events:
        events.append(
            _matched_event(
                scope=scope,
                trigger=trigger,
                detected_at=detected_at,
                id_factory=id_factory,
            )
        )
    return events


def _matched_event(
    *,
    scope: str,
    trigger: ReconciliationTrigger,
    detected_at: datetime,
    id_factory: IdFactory,
) -> ReconciliationEvent:
    return ReconciliationEvent(
        event_id=id_factory.new_id("REC"),
        scope=scope,
        trigger=trigger,
        difference_class=DifferenceClass.NONE,
        severity=Severity.INFO,
        reason_code=ReasonCode.OK,
        entries_blocked=False,
        detected_at=detected_at,
        resolved_at=detected_at,
    )
