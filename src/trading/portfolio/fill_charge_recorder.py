"""Persist and compute per-fill charges on confirmed broker fills."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_CEILING, Decimal

from trading.config.charge_policy import ChargePolicyConfig
from trading.domain.contracts.fill_charges import (
    ChargeCalculationInputs,
    FillChargeComponents,
    FillChargeRecord,
)
from trading.domain.contracts.order import OrderEvent
from trading.domain.enums import OrderState, Side
from trading.domain.primitives import Currency, Money, Rounding
from trading.portfolio.fill_ledger import is_chargeable_fill, order_fill_dedupe_key
from trading.storage.trading_store import TradingStore

__all__ = [
    "backfill_fill_charges",
    "compute_fill_charges",
    "record_fill_charge",
]


def compute_fill_charges(
    event: OrderEvent,
    *,
    policy: ChargePolicyConfig,
    contracts_per_lot: int,
    recorded_at: datetime,
) -> FillChargeRecord:
    """Decompose statutory charges for one confirmed fill."""
    if not is_chargeable_fill(event):
        raise ValueError(f"order {event.identity.idempotency_key} is not chargeable")
    currency = Currency.INR
    premium = event.average_fill_price.value
    qty = Decimal(event.filled_quantity)
    turnover = (premium * qty).quantize(Decimal("0.01"))
    brokerage = policy.brokerage_per_order.to_money()
    exchange = (
        turnover
        * (
            policy.nse_transaction_fraction
            + policy.clearing_fraction
            + policy.sebi_turnover_fraction
            + policy.ipft_options_fraction
        )
    ).quantize(Decimal("0.01"), rounding=ROUND_CEILING)
    taxes = Decimal(0)
    if event.command.side is Side.SELL:
        taxes = (turnover * policy.stt_sell_fraction).quantize(
            Decimal("0.01"), rounding=ROUND_CEILING
        )
    stamp = Decimal(0)
    if event.command.side is Side.BUY:
        stamp = (turnover * policy.stamp_duty_buy_fraction).quantize(
            Decimal("0.01"), rounding=ROUND_CEILING
        )
    taxable = brokerage.amount + exchange
    gst = (taxable * policy.gst_fraction).quantize(
        Decimal("0.01"), rounding=ROUND_CEILING
    )
    components = FillChargeComponents(
        brokerage=brokerage,
        exchange_fees=Money.of(exchange, currency),
        taxes=Money.of(taxes, currency),
        gst=Money.of(gst, currency),
        stamp_duty=Money.of(stamp, currency),
    )
    total = components.total.quantized(Rounding.CEILING)
    inputs = ChargeCalculationInputs(
        trade_id=event.identity.trade_id,
        order_event_id=event.event_id,
        side=event.command.side,
        filled_quantity=event.filled_quantity,
        contracts_per_lot=contracts_per_lot,
        premium_per_contract=str(premium),
        turnover=str(turnover),
    )
    return FillChargeRecord(
        fill_idempotency_key=order_fill_dedupe_key(event),
        trade_id=event.identity.trade_id,
        order_event_id=event.event_id,
        policy_version=policy.policy_version,
        components=components,
        total_charges=total,
        inputs=inputs,
        recorded_at=recorded_at,
    )


def record_fill_charge(
    store: TradingStore,
    event: OrderEvent,
    *,
    policy: ChargePolicyConfig,
    contracts_per_lot: int,
    recorded_at: datetime | None = None,
) -> FillChargeRecord | None:
    """Persist charges once per fill identity; replays and terminal non-fills skip."""
    if not is_chargeable_fill(event):
        return None
    key = order_fill_dedupe_key(event)
    existing = store.get_fill_charge(key)
    if existing is not None:
        if (
            existing.inputs.filled_quantity >= event.filled_quantity
            and existing.order_event_id == event.event_id
        ):
            return existing
        if existing.inputs.filled_quantity > event.filled_quantity:
            return existing
    if recorded_at is None:
        raise ValueError("recorded_at is required for durable fill charges")
    stamp = recorded_at
    record = compute_fill_charges(
        event,
        policy=policy,
        contracts_per_lot=contracts_per_lot,
        recorded_at=stamp,
    )
    return store.upsert_fill_charge(record, event_id=record.order_event_id)


def backfill_fill_charges(
    store: TradingStore,
    orders_by_id: dict[str, OrderEvent],
    *,
    policy: ChargePolicyConfig,
    contracts_per_lot_by_trade: dict[str, int],
    default_contracts_per_lot: int,
) -> None:
    """Ensure every chargeable fill in the index has a durable charge row."""
    for event in orders_by_id.values():
        if not is_chargeable_fill(event):
            continue
        lot_size = contracts_per_lot_by_trade.get(
            event.identity.trade_id, default_contracts_per_lot
        )
        record_fill_charge(
            store,
            event,
            policy=policy,
            contracts_per_lot=lot_size,
            recorded_at=event.received_at,
        )
