"""Fill-ledger cash-flow reconstruction shared by mode and campaign ledgers."""

from __future__ import annotations

from decimal import Decimal

from trading.domain.contracts.order import OrderEvent
from trading.domain.enums import OrderState, Side
from trading.domain.primitives import Currency, Money, Rounding

__all__ = ["trade_fill_cash_flow"]


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
        if order.state not in {OrderState.FILLED, OrderState.PARTIAL}:
            continue
        if order.average_fill_price is None or order.filled_quantity <= 0:
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
