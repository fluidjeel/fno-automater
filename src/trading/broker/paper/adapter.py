"""Deterministic paper broker: immediate full fill at limit price (slice 1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from trading.broker.paper.fixtures import PaperBrokerFixtures
from trading.broker.ports import (
    BrokerError,
    BrokerFunds,
    BrokerSubmitRequest,
    DuplicateBrokerOrderError,
    MarginPreviewRequest,
    MarginPreviewResult,
)
from trading.domain.clock import Clock
from trading.domain.contracts.order import OrderCommand, OrderEvent
from trading.domain.contracts.portfolio import (
    PendingOrderSummary,
    PositionRecord,
)
from trading.domain.enums import OrderState, OrderType, Side
from trading.domain.ids import IdFactory
from trading.domain.primitives import Money, Price

__all__ = ["PaperBroker", "margin_preview_key"]


def margin_preview_key(symbol: str, side: Side, quantity_contracts: int) -> str:
    """Stable lookup key for fixture-backed margin previews."""
    return f"{symbol}|{side.value}|{quantity_contracts}"


def _position_key(trade_id: str, symbol: str) -> str:
    return f"{trade_id}|{symbol}"


@dataclass
class _PaperState:
    """Mutable broker mirror used by the paper adapter."""

    funds: BrokerFunds
    positions: dict[str, PositionRecord] = field(default_factory=dict)
    orders_by_internal_id: dict[str, OrderEvent] = field(default_factory=dict)
    orders_by_idempotency_key: dict[str, OrderEvent] = field(default_factory=dict)


class PaperBroker:
    """Offline broker implementing immediate full fills for LIMIT orders."""

    def __init__(
        self,
        *,
        clock: Clock,
        id_factory: IdFactory,
        fixtures: PaperBrokerFixtures,
    ) -> None:
        self._clock = clock
        self._ids = id_factory
        self._fixtures = fixtures
        self._state = _PaperState(
            funds=fixtures.account,
            positions={
                _position_key(position.trade_id, position.contract.symbol): position
                for position in fixtures.positions
            },
        )

    @classmethod
    def from_fixtures(
        cls,
        root: Path,
        *,
        clock: Clock,
        id_factory: IdFactory,
    ) -> PaperBroker:
        """Construct a paper broker from tests/fixtures/broker/."""
        return cls(
            clock=clock,
            id_factory=id_factory,
            fixtures=PaperBrokerFixtures.load(root),
        )

    def submit(self, request: BrokerSubmitRequest) -> OrderEvent:
        """Submit one order and fill immediately at the limit price."""
        order = request.order
        key = order.identity.idempotency_key
        existing = self._state.orders_by_idempotency_key.get(key)
        if existing is not None:
            if existing.identity.internal_order_id != order.identity.internal_order_id:
                raise DuplicateBrokerOrderError(key, existing)
            return existing

        fill_price = self._resolve_fill_price(order.command)
        now = self._clock.now_utc()
        broker_order_id = self._ids.new_id("PBRK")
        event = OrderEvent.model_validate(
            {
                "event_id": self._ids.new_id("EVT"),
                "identity": {
                    **order.identity.model_dump(mode="python"),
                    "broker_order_id": broker_order_id,
                },
                "command": order.command.model_dump(mode="python"),
                "state": OrderState.FILLED,
                "attempt_number": request.attempt_number,
                "acknowledged_quantity": order.command.quantity_contracts,
                "filled_quantity": order.command.quantity_contracts,
                "average_fill_price": fill_price,
                "sent_at": now,
                "received_at": now,
                "broker_time": now,
                "raw_broker_status": "FILLED",
                "raw_payload_ref": f"paper://orders/{broker_order_id}",
            }
        )
        self._apply_fill(request, event, fill_price)
        self._state.orders_by_internal_id[order.identity.internal_order_id] = event
        self._state.orders_by_idempotency_key[key] = event
        return event

    def cancel(self, internal_order_id: str) -> OrderEvent:
        """Cancel a working order. Filled orders cannot be cancelled."""
        existing = self._state.orders_by_internal_id.get(internal_order_id)
        if existing is None:
            raise BrokerError(f"unknown order: {internal_order_id}")
        if existing.state.is_terminal:
            raise BrokerError(
                f"order {internal_order_id} is terminal ({existing.state})"
            )
        now = self._clock.now_utc()
        cancelled = OrderEvent.model_validate(
            {
                **existing.model_dump(mode="python"),
                "event_id": self._ids.new_id("EVT"),
                "state": OrderState.CANCELLED,
                "attempt_number": existing.attempt_number + 1,
                "sent_at": now,
                "received_at": now,
                "broker_time": now,
                "raw_broker_status": "CANCELLED",
            }
        )
        self._state.orders_by_internal_id[internal_order_id] = cancelled
        key = existing.identity.idempotency_key
        self._state.orders_by_idempotency_key[key] = cancelled
        return cancelled

    def get_order(self, internal_order_id: str) -> OrderEvent | None:
        return self._state.orders_by_internal_id.get(internal_order_id)

    def list_orders(self) -> tuple[OrderEvent, ...]:
        return tuple(self._state.orders_by_internal_id.values())

    def get_positions(self) -> tuple[PositionRecord, ...]:
        return tuple(self._state.positions.values())

    def get_pending_orders(self) -> tuple[PendingOrderSummary, ...]:
        pending: list[PendingOrderSummary] = []
        for event in self._state.orders_by_internal_id.values():
            if not event.state.is_working:
                continue
            pending.append(
                PendingOrderSummary(
                    internal_order_id=event.identity.internal_order_id,
                    intent_id=event.identity.intent_id,
                    contract=event.command.contract,
                    side=event.command.side,
                    quantity_contracts=event.outstanding_quantity,
                    state=event.state,
                )
            )
        return tuple(pending)

    def get_funds(self) -> BrokerFunds:
        return self._state.funds.model_copy(
            update={"as_of": self._clock.now_utc()},
        )

    def preview_margin(self, request: MarginPreviewRequest) -> MarginPreviewResult:
        """Return fixture-backed margin preview; fail closed when unconfirmed."""
        if not request.legs:
            raise BrokerError("margin preview requires at least one leg")
        currency = self._state.funds.equity.currency
        total = Money.zero(currency)
        all_confirmed = True
        for leg in request.legs:
            key = margin_preview_key(
                leg.contract.symbol,
                leg.side,
                leg.quantity_contracts,
            )
            preview = self._fixtures.margin_previews.get(key)
            if preview is None or not preview.confirmed:
                all_confirmed = False
                continue
            total = total + preview.margin_required
        if not all_confirmed or total.is_zero:
            return MarginPreviewResult(
                request_id=request.request_id,
                as_of=self._clock.now_utc(),
                margin_required=Money.zero(currency),
                margin_available_after=self._state.funds.margin_available,
                confirmed=False,
            )
        return MarginPreviewResult(
            request_id=request.request_id,
            as_of=self._clock.now_utc(),
            margin_required=total.quantized(),
            margin_available_after=self._state.funds.margin_available - total,
            confirmed=True,
        )

    def _apply_fill(
        self,
        request: BrokerSubmitRequest,
        event: OrderEvent,
        fill_price: Price,
    ) -> None:
        command = event.command
        trade_id = event.identity.trade_id
        currency = self._state.funds.equity.currency
        position_key = _position_key(trade_id, command.contract.symbol)
        existing = self._state.positions.get(position_key)
        closing = existing is not None and existing.side is not command.side
        if existing is None:
            self._state.positions[position_key] = PositionRecord(
                trade_id=trade_id,
                strategy_id=request.strategy_id,
                contract=command.contract,
                side=command.side,
                quantity_contracts=command.quantity_contracts,
                average_price=fill_price,
                unrealized_pnl=Money.zero(currency),
            )
        elif closing:
            remaining = existing.quantity_contracts - command.quantity_contracts
            if command.quantity_contracts > existing.quantity_contracts:
                raise BrokerError("close quantity exceeds open position")
            if remaining == 0:
                del self._state.positions[position_key]
            else:
                self._state.positions[position_key] = existing.model_copy(
                    update={"quantity_contracts": remaining},
                )
        else:
            self._state.positions[position_key] = self._merge_position(
                existing,
                command.quantity_contracts,
                fill_price,
            )

        if closing and existing is not None:
            margin_delta = self._margin_delta(
                command,
                command.quantity_contracts,
                position_side=existing.side,
            )
        else:
            margin_delta = self._margin_delta(command, command.quantity_contracts)
        funds = self._state.funds
        if closing:
            self._state.funds = funds.model_copy(
                update={
                    "as_of": event.received_at,
                    "margin_used": funds.margin_used - margin_delta,
                    "margin_available": funds.margin_available + margin_delta,
                }
            )
        else:
            self._state.funds = funds.model_copy(
                update={
                    "as_of": event.received_at,
                    "margin_used": funds.margin_used + margin_delta,
                    "margin_available": funds.margin_available - margin_delta,
                }
            )

    def _margin_delta(
        self,
        command: OrderCommand,
        quantity_contracts: int,
        *,
        position_side: Side | None = None,
    ) -> Money:
        lookup_side = position_side if position_side is not None else command.side
        preview_key = margin_preview_key(
            command.contract.symbol,
            lookup_side,
            quantity_contracts,
        )
        preview = self._fixtures.margin_previews.get(preview_key)
        currency = self._state.funds.equity.currency
        if preview is not None and preview.confirmed:
            return preview.margin_required
        notional = command.limit_price
        if notional is None:
            raise BrokerError("LIMIT order requires a limit price")
        return Money.of(str(notional.value * Decimal(quantity_contracts)), currency)

    @staticmethod
    def _merge_position(
        existing: PositionRecord,
        quantity_contracts: int,
        fill_price: Price,
    ) -> PositionRecord:
        total_qty = existing.quantity_contracts + quantity_contracts
        weighted = (
            existing.average_price.value * Decimal(existing.quantity_contracts)
            + fill_price.value * Decimal(quantity_contracts)
        ) / Decimal(total_qty)
        average_price = Price.snap(weighted, existing.average_price.tick)
        return existing.model_copy(
            update={
                "quantity_contracts": total_qty,
                "average_price": average_price,
            }
        )

    @staticmethod
    def _resolve_fill_price(command: OrderCommand) -> Price:
        if command.order_type is not OrderType.LIMIT:
            raise BrokerError(
                f"paper broker slice 1 supports LIMIT orders only, got "
                f"{command.order_type}"
            )
        if command.limit_price is None:
            raise BrokerError("LIMIT order requires a limit price")
        return command.limit_price
