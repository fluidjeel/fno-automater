"""Fill-ledger cash-flow and charge reconstruction shared by ledgers."""

from __future__ import annotations

from decimal import Decimal

from trading.domain.contracts.fill_charges import FillChargeRecord
from trading.domain.contracts.order import OrderEvent
from trading.domain.enums import OrderState, Side
from trading.domain.primitives import Currency, Money, Rounding
from trading.storage.trading_store import TradingEventType, TradingStore

__all__ = [
    "index_fill_charges",
    "index_order_events",
    "is_chargeable_fill",
    "order_fill_dedupe_key",
    "trade_confirmed_charges",
    "trade_fill_cash_flow",
]

_CHARGEABLE_STATES = frozenset({OrderState.FILLED, OrderState.PARTIAL})


def is_chargeable_fill(event: OrderEvent) -> bool:
    """Return whether cash-flow and charge reconstruction may use this event."""
    if event.state not in _CHARGEABLE_STATES:
        return False
    if event.filled_quantity <= 0 or event.average_fill_price is None:
        return False
    return True


def order_fill_dedupe_key(event: OrderEvent) -> str:
    """Stable fill identity shared by cash-flow and charge deduplication."""
    return event.identity.idempotency_key or event.identity.internal_order_id


def index_order_events(store: TradingStore) -> dict[str, OrderEvent]:
    """Latest order event per fill identity."""
    indexed: dict[str, OrderEvent] = {}
    for stored in store.read_events():
        if stored.event_type is not TradingEventType.ORDER_EVENT:
            continue
        evt = stored.deserialize()
        if not isinstance(evt, OrderEvent):
            continue
        key = order_fill_dedupe_key(evt)
        current = indexed.get(key)
        if current is None or evt.received_at >= current.received_at:
            indexed[key] = evt
    return indexed


def index_fill_charges(store: TradingStore) -> dict[str, FillChargeRecord]:
    """Fill charges keyed by the same identity as :func:`order_fill_dedupe_key`."""
    return {record.fill_idempotency_key: record for record in store.list_fill_charges()}


def trade_fill_cash_flow(
    trade_id: str,
    orders_by_id: dict[str, OrderEvent],
    currency: Currency,
) -> Money:
    """Sum signed cash flows for each deduped fill belonging to one trade."""
    net_pnl_decimal = Decimal(0)
    saw_fill = False
    for order in orders_by_id.values():
        if order.identity.trade_id != trade_id:
            continue
        if not is_chargeable_fill(order):
            continue
        saw_fill = True
        fill_val = order.average_fill_price.value * Decimal(order.filled_quantity)
        if order.command.side is Side.SELL:
            net_pnl_decimal += fill_val
        else:
            net_pnl_decimal -= fill_val
    if not saw_fill:
        return Money.zero(currency)
    return Money.of(str(net_pnl_decimal), currency).quantized(Rounding.HALF_EVEN)


def trade_confirmed_charges(
    trade_id: str,
    orders_by_id: dict[str, OrderEvent],
    charges_by_key: dict[str, FillChargeRecord],
    currency: Currency,
) -> Money:
    """Sum durable charges for deduped fills on one trade."""
    total = Decimal(0)
    seen: set[str] = set()
    for order in orders_by_id.values():
        if order.identity.trade_id != trade_id or not is_chargeable_fill(order):
            continue
        key = order_fill_dedupe_key(order)
        if key in seen:
            continue
        seen.add(key)
        record = charges_by_key.get(key)
        if record is not None:
            total += record.total_charges.amount
    return Money.of(str(total), currency).quantized(Rounding.HALF_EVEN)
