"""Deterministic paper broker: LIMIT fills, optional conservative re-price."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from trading.analytics.fills import simulate_fill
from trading.broker.paper.fixtures import PaperBrokerFixtures
from trading.broker.ports import (
    BrokerError,
    BrokerFunds,
    BrokerSubmitRequest,
    DuplicateBrokerOrderError,
    MarginPreviewLeg,
    MarginPreviewRequest,
    MarginPreviewResult,
)
from trading.config.evaluation import FillModelConfig
from trading.domain.clock import Clock
from trading.domain.contracts.order import OrderCommand, OrderEvent
from trading.domain.contracts.portfolio import (
    PendingOrderSummary,
    PositionRecord,
)
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import (
    FillOutcome,
    InstrumentKind,
    OrderState,
    OrderType,
    Side,
)
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
    """Offline broker. Immediate LIMIT fills unless a conservative fill model is set."""

    def __init__(
        self,
        *,
        clock: Clock,
        id_factory: IdFactory,
        fixtures: PaperBrokerFixtures,
        fill_model: FillModelConfig | None = None,
        synthetic_margin: bool = False,
        future_margin_fraction: Decimal | None = None,
    ) -> None:
        self._clock = clock
        self._ids = id_factory
        self._fixtures = fixtures
        self._fill_model = fill_model
        self._synthetic_margin = synthetic_margin
        self._future_margin_fraction = future_margin_fraction
        self._quotes: dict[str, MarketQuote] = {}
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
        fill_model: FillModelConfig | None = None,
    ) -> PaperBroker:
        """Construct a paper broker from tests/fixtures/broker/."""
        return cls(
            clock=clock,
            id_factory=id_factory,
            fixtures=PaperBrokerFixtures.load(root),
            fill_model=fill_model,
        )

    @classmethod
    def for_session(
        cls,
        *,
        clock: Clock,
        id_factory: IdFactory,
        funds: BrokerFunds,
        fill_model: FillModelConfig | None = None,
        future_margin_fraction: Decimal | None = None,
    ) -> PaperBroker:
        """Live-symbol paper broker: empty fixtures, synthetic margin previews."""
        return cls(
            clock=clock,
            id_factory=id_factory,
            fixtures=PaperBrokerFixtures(
                account=funds,
                positions=(),
                margin_previews={},
            ),
            fill_model=fill_model,
            synthetic_margin=True,
            future_margin_fraction=future_margin_fraction,
        )

    def publish_quote(self, symbol: str, quote: MarketQuote) -> None:
        """Publish the decision-time book used by the conservative fill model."""
        self._quotes[symbol] = quote

    @property
    def fill_model(self) -> FillModelConfig | None:
        """Conservative fill model, or None for immediate limit fills."""
        return self._fill_model

    def submit(self, request: BrokerSubmitRequest) -> OrderEvent:
        """Submit one order; fill immediately or via the conservative model."""
        order = request.order
        key = order.identity.idempotency_key
        existing = self._state.orders_by_idempotency_key.get(key)
        if existing is not None:
            if existing.identity.internal_order_id != order.identity.internal_order_id:
                raise DuplicateBrokerOrderError(key, existing)
            return existing

        now = self._clock.now_utc()
        broker_order_id = self._ids.new_id("PBRK")
        if self._fill_model is None:
            fill_price = self._resolve_limit_price(order.command)
            event = self._filled_event(
                request, broker_order_id=broker_order_id, fill_price=fill_price, now=now
            )
            self._apply_fill(request, event, fill_price)
        else:
            event = self._conservative_event(
                request, broker_order_id=broker_order_id, now=now
            )
            if (
                event.state is OrderState.FILLED
                and event.average_fill_price is not None
            ):
                self._apply_fill(request, event, event.average_fill_price)
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
            if preview is not None and preview.confirmed:
                total = total + preview.margin_required
                continue
            estimated = self._synthetic_leg_margin(leg)
            if estimated is None:
                all_confirmed = False
                continue
            total = total + estimated
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

    def _synthetic_leg_margin(self, leg: MarginPreviewLeg) -> Money | None:
        """PAPER-only estimate when the fixture table has no live weekly key."""
        if not self._synthetic_margin:
            return None
        quote = self._quotes.get(leg.contract.symbol)
        currency = self._state.funds.equity.currency
        qty = Decimal(leg.quantity_contracts)
        if leg.contract.instrument_kind is InstrumentKind.FUTURE:
            if self._future_margin_fraction is None:
                return None
            notional = _quote_notional(quote)
            if notional is None:
                return None
            amount = notional * qty * self._future_margin_fraction
            return Money.of(str(amount), currency)
        premium = _option_premium(quote, leg.side)
        if premium is None:
            return None
        return Money.of(str(premium * qty), currency)

    def dump_state(self) -> dict[str, object]:
        """Serialize funds, positions and idempotent orders for process restart."""
        return {
            "funds": self._state.funds.model_dump(mode="json"),
            "positions": [
                position.model_dump(mode="json")
                for position in self._state.positions.values()
            ],
            "orders": [
                event.model_dump(mode="json")
                for event in self._state.orders_by_idempotency_key.values()
            ],
        }

    def load_state(self, payload: dict[str, object]) -> None:
        """Restore a dump from a previous PAPER process."""
        funds_raw = payload.get("funds")
        if not isinstance(funds_raw, dict):
            raise BrokerError("broker state dump is missing funds")
        self._state.funds = BrokerFunds.model_validate(funds_raw)
        positions_raw = payload.get("positions", [])
        if not isinstance(positions_raw, list):
            raise BrokerError("broker state dump positions must be a list")
        self._state.positions = {}
        for row in positions_raw:
            if not isinstance(row, dict):
                continue
            position = PositionRecord.model_validate(row)
            self._state.positions[
                _position_key(position.trade_id, position.contract.symbol)
            ] = position
        orders_raw = payload.get("orders", [])
        if not isinstance(orders_raw, list):
            raise BrokerError("broker state dump orders must be a list")
        self._state.orders_by_idempotency_key = {}
        self._state.orders_by_internal_id = {}
        for row in orders_raw:
            if not isinstance(row, dict):
                continue
            event = OrderEvent.model_validate(row)
            key = event.identity.idempotency_key
            self._state.orders_by_idempotency_key[key] = event
            self._state.orders_by_internal_id[event.identity.internal_order_id] = event

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
            # A missing margin preview prices the close off the exit limit.
            # That is not the margin posted at entry, so release only what
            # is still in use.
            if margin_delta.amount > funds.margin_used.amount:
                margin_delta = funds.margin_used
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
    def _resolve_limit_price(command: OrderCommand) -> Price:
        if command.order_type is not OrderType.LIMIT:
            raise BrokerError(
                f"paper broker supports LIMIT orders only, got {command.order_type}"
            )
        if command.limit_price is None:
            raise BrokerError("LIMIT order requires a limit price")
        return command.limit_price

    def _filled_event(
        self,
        request: BrokerSubmitRequest,
        *,
        broker_order_id: str,
        fill_price: Price,
        now: datetime,
    ) -> OrderEvent:
        order = request.order
        return OrderEvent.model_validate(
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

    def _conservative_event(
        self,
        request: BrokerSubmitRequest,
        *,
        broker_order_id: str,
        now: datetime,
    ) -> OrderEvent:
        command = request.order.command
        self._resolve_limit_price(command)
        policy = self._fill_model
        if policy is None:
            raise BrokerError("conservative fill requires a fill model")
        quote = self._quotes.get(command.contract.symbol)
        if quote is None:
            return self._rejected_event(
                request,
                broker_order_id=broker_order_id,
                now=now,
                reason_code="PRICE_UNAVAILABLE",
                detail="conservative paper fill requires a published quote",
            )
        simulation = simulate_fill(command, quote, policy=policy)
        if (
            simulation.outcome is FillOutcome.FILLED
            and simulation.fill_price is not None
        ):
            event = self._filled_event(
                request,
                broker_order_id=broker_order_id,
                fill_price=simulation.fill_price,
                now=now,
            )
            return event.model_copy(
                update={
                    "reason_code": simulation.reason_code,
                    "reason_detail": simulation.reason_detail,
                }
            )
        return self._rejected_event(
            request,
            broker_order_id=broker_order_id,
            now=now,
            reason_code=simulation.reason_code.value,
            detail=simulation.reason_detail or simulation.outcome.value,
        )

    def _rejected_event(
        self,
        request: BrokerSubmitRequest,
        *,
        broker_order_id: str,
        now: datetime,
        reason_code: str,
        detail: str,
    ) -> OrderEvent:
        order = request.order
        return OrderEvent.model_validate(
            {
                "event_id": self._ids.new_id("EVT"),
                "identity": {
                    **order.identity.model_dump(mode="python"),
                    "broker_order_id": broker_order_id,
                },
                "command": order.command.model_dump(mode="python"),
                "state": OrderState.REJECTED,
                "attempt_number": request.attempt_number,
                "acknowledged_quantity": 0,
                "filled_quantity": 0,
                "average_fill_price": None,
                "sent_at": now,
                "received_at": now,
                "broker_time": now,
                "reason_code": reason_code,
                "reason_detail": detail,
                "raw_broker_status": "REJECTED",
                "raw_payload_ref": f"paper://orders/{broker_order_id}",
            }
        )


def _quote_notional(quote: MarketQuote | None) -> Decimal | None:
    if quote is None:
        return None
    for price in (quote.last, quote.ask, quote.bid):
        if price is not None:
            return price.value
    return None


def _option_premium(quote: MarketQuote | None, side: Side) -> Decimal | None:
    if quote is None:
        return None
    if side is Side.BUY and quote.ask is not None:
        return quote.ask.value
    if side is Side.SELL and quote.bid is not None:
        return quote.bid.value
    if quote.last is not None:
        return quote.last.value
    return None
