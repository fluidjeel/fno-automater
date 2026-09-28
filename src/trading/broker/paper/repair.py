"""Rebuild missing PAPER broker positions and orders from persisted fill events."""

from __future__ import annotations

from trading.broker.paper.adapter import PaperBroker
from trading.domain.clock import Clock
from trading.domain.contracts import PositionLifecycleRecord, ReconciliationEvent
from trading.domain.contracts.order import OrderEvent
from trading.domain.enums import (
    DifferenceClass,
    OrderState,
    ReasonCode,
    ReconciliationTrigger,
    Severity,
    TradeState,
)
from trading.domain.ids import IdFactory
from trading.storage.trading_store import TradingEventType, TradingStore

__all__ = ["repair_paper_broker_from_store"]


def repair_paper_broker_from_store(
    broker: PaperBroker,
    store: TradingStore,
    *,
    id_factory: IdFactory,
    clock: Clock,
) -> tuple[ReconciliationEvent, ...]:
    """Import missing broker legs/orders for OPEN lifecycles from trading_events."""
    repaired: list[ReconciliationEvent] = []
    now = clock.now_utc()
    for record in store.list_position_lifecycle():
        if record.position.state is TradeState.CLOSED:
            continue
        if not _broker_legs_mismatch(broker, record):
            continue
        _import_filled_orders(broker, store, record.trade_id)
        broker.import_positions_from_lifecycle(record)
        event = ReconciliationEvent(
            event_id=id_factory.new_id("REC"),
            scope=f"trade/{record.trade_id}/lifecycle",
            trigger=ReconciliationTrigger.BOOT,
            expected_local_ref=record.trade_id,
            observed_broker_ref=record.trade_id,
            difference_class=DifferenceClass.MISSING_LOCAL_EVENT,
            severity=Severity.INFO,
            reason_code=ReasonCode.UNRECONCILED_POSITION,
            repair_action=(
                "rebuilt PAPER broker_state positions and filled orders from "
                "trading_events after crash before broker_state persist"
            ),
            repair_succeeded=True,
            entries_blocked=False,
            detected_at=now,
            resolved_at=now,
        )
        store.append(
            TradingEventType.RECONCILIATION_EVENT,
            event,
            event_id=event.event_id,
        )
        repaired.append(event)
    if repaired:
        broker.persist_state()
    return tuple(repaired)


def _broker_legs_mismatch(broker: PaperBroker, record: PositionLifecycleRecord) -> bool:
    local_keys = {
        (leg.contract.symbol, leg.side, leg.quantity_contracts)
        for leg in record.position.legs
    }
    broker_keys = {
        (position.contract.symbol, position.side, position.quantity_contracts)
        for position in broker.get_positions()
        if position.trade_id == record.trade_id
    }
    return local_keys != broker_keys


def _import_filled_orders(
    broker: PaperBroker, store: TradingStore, trade_id: str
) -> None:
    strategy_id = ""
    for record in store.list_position_lifecycle():
        if record.trade_id == trade_id:
            strategy_id = record.intent.strategy_id
            break
    for stored in store.read_events():
        if stored.event_type is not TradingEventType.ORDER_EVENT:
            continue
        event = stored.deserialize()
        if not isinstance(event, OrderEvent):
            continue
        if event.identity.trade_id != trade_id:
            continue
        if event.state is not OrderState.FILLED:
            continue
        broker.import_order_event(event, strategy_id=strategy_id)
