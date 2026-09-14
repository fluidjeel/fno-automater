"""Live Fyers broker adapter implementing Layer 2 broker ports."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from trading.broker.fyers.client import FyersApiError, FyersTransactionClient
from trading.broker.fyers.mapping import (
    build_place_order_payload,
    parse_funds,
    parse_order_event,
    parse_pending_order,
    parse_positions,
    to_fyers_symbol,
)
from trading.broker.ports import (
    BrokerError,
    BrokerFunds,
    BrokerSubmitRequest,
    BrokerSubmitTimeoutError,
    DuplicateBrokerOrderError,
    MarginPreviewLeg,
    MarginPreviewRequest,
    MarginPreviewResult,
)
from trading.domain.clock import Clock
from trading.domain.contracts.order import OrderEvent
from trading.domain.contracts.portfolio import PendingOrderSummary, PositionRecord
from trading.domain.enums import OrderState, ReasonCode, Side
from trading.domain.ids import IdFactory
from trading.domain.primitives import Currency, Money

_INR = Currency.INR

__all__ = ["FyersBroker", "FyersBrokerConfig"]

_PLACE_UNACKED = 201
_CANCEL_ACK = 1103
_STATUS_CANCELLED = 1
_STATUS_REJECTED = 5


class FyersBrokerConfig:
    """Verified broker settings for the Fyers adapter."""

    __slots__ = (
        "account_id",
        "margin_preview_verified",
        "offline_order",
        "product_type",
        "strategy_id",
    )

    def __init__(
        self,
        *,
        account_id: str,
        strategy_id: str,
        product_type: str = "MARGIN",
        offline_order: bool = False,
        margin_preview_verified: bool = False,
    ) -> None:
        self.account_id = account_id
        self.strategy_id = strategy_id
        self.product_type = product_type
        self.offline_order = offline_order
        self.margin_preview_verified = margin_preview_verified


class FyersBroker:
    """Fyers v3 broker adapter with idempotent submit and fail-closed margin."""

    def __init__(
        self,
        client: FyersTransactionClient,
        config: FyersBrokerConfig,
        *,
        clock: Clock,
        id_factory: IdFactory,
    ) -> None:
        self._client = client
        self._config = config
        self._clock = clock
        self._ids = id_factory
        self._orders_by_internal_id: dict[str, OrderEvent] = {}
        self._orders_by_idempotency_key: dict[str, OrderEvent] = {}

    def submit(self, request: BrokerSubmitRequest) -> OrderEvent:
        """Submit one planned order via Fyers `/orders/sync`."""
        order = request.order
        key = order.identity.idempotency_key
        existing = self._orders_by_idempotency_key.get(key)
        if existing is not None:
            if existing.identity.internal_order_id != order.identity.internal_order_id:
                raise DuplicateBrokerOrderError(key, existing)
            return existing

        payload = build_place_order_payload(
            order.command,
            client_order_id=order.identity.client_order_id,
            product_type=self._config.product_type,
            offline_order=self._config.offline_order,
        )
        sent_at = self._clock.now_utc()
        try:
            response = self._client.place_order(payload)
        except FyersApiError as exc:
            raise BrokerError(str(exc)) from exc

        received_at = self._clock.now_utc()
        code = _int_code(response.get("code"))
        if response.get("s") != "ok":
            return self._remember(
                self._terminal_event(
                    request,
                    state=OrderState.REJECTED,
                    received_at=received_at,
                    sent_at=sent_at,
                    raw_status=str(_STATUS_REJECTED),
                    detail=str(response.get("message", "order rejected")),
                )
            )

        if code == _PLACE_UNACKED:
            unknown = self._terminal_event(
                request,
                state=OrderState.UNKNOWN,
                received_at=received_at,
                sent_at=sent_at,
                raw_status="UNKNOWN",
                detail="broker returned code 201; outcome unknown until reconciliation",
            )
            self._remember(unknown)
            raise BrokerSubmitTimeoutError(key)

        broker_order_id = response.get("id")
        if not isinstance(broker_order_id, str) or not broker_order_id:
            unknown = self._terminal_event(
                request,
                state=OrderState.UNKNOWN,
                received_at=received_at,
                sent_at=sent_at,
                raw_status="UNKNOWN",
                detail="place response missing broker order id",
            )
            self._remember(unknown)
            raise BrokerSubmitTimeoutError(key)

        row = self._fetch_order_row(broker_order_id)
        if row is None:
            unknown = self._terminal_event(
                request,
                state=OrderState.UNKNOWN,
                received_at=received_at,
                sent_at=sent_at,
                raw_status="UNKNOWN",
                detail=f"order {broker_order_id} missing from orderbook",
            )
            self._remember(unknown)
            raise BrokerSubmitTimeoutError(key)

        event = parse_order_event(
            row,
            identity=order.identity,
            command=order.command,
            attempt_number=request.attempt_number,
            event_id=self._ids.new_id("EVT"),
            received_at=received_at,
            sent_at=sent_at,
        )
        return self._remember(event)

    def cancel(self, internal_order_id: str) -> OrderEvent:
        """Cancel a working order by internal id."""
        existing = self._orders_by_internal_id.get(internal_order_id)
        if existing is None:
            raise BrokerError(f"unknown order: {internal_order_id}")
        if existing.state.is_terminal:
            raise BrokerError(
                f"order {internal_order_id} is terminal ({existing.state})"
            )
        broker_order_id = existing.identity.broker_order_id
        if broker_order_id is None:
            raise BrokerError(f"order {internal_order_id} has no broker_order_id")

        sent_at = self._clock.now_utc()
        try:
            response = self._client.cancel_order(broker_order_id)
        except FyersApiError as exc:
            raise BrokerError(str(exc)) from exc
        received_at = self._clock.now_utc()
        if response.get("s") != "ok" or _int_code(response.get("code")) != _CANCEL_ACK:
            raise BrokerError(
                f"cancel failed for {broker_order_id}: {response.get('message')}"
            )

        row = self._fetch_order_row(broker_order_id)
        if row is None:
            cancelled = existing.model_copy(
                update={
                    "event_id": self._ids.new_id("EVT"),
                    "state": OrderState.CANCELLED,
                    "attempt_number": existing.attempt_number + 1,
                    "sent_at": sent_at,
                    "received_at": received_at,
                    "broker_time": received_at,
                    "raw_broker_status": str(_STATUS_CANCELLED),
                }
            )
            return self._remember(cancelled)

        cancelled = parse_order_event(
            row,
            identity=existing.identity,
            command=existing.command,
            attempt_number=existing.attempt_number + 1,
            event_id=self._ids.new_id("EVT"),
            received_at=received_at,
            sent_at=sent_at,
        )
        return self._remember(cancelled)

    def get_order(self, internal_order_id: str) -> OrderEvent | None:
        return self._orders_by_internal_id.get(internal_order_id)

    def list_orders(self) -> tuple[OrderEvent, ...]:
        return tuple(self._orders_by_internal_id.values())

    def get_positions(self) -> tuple[PositionRecord, ...]:
        received_at = self._clock.now_utc()
        try:
            payload = self._client.positions()
        except FyersApiError as exc:
            raise BrokerError(str(exc)) from exc
        if payload.get("s") != "ok":
            raise BrokerError(payload.get("message", "positions query failed"))
        return parse_positions(
            payload,
            strategy_id=self._config.strategy_id,
            as_of=received_at,
        )

    def get_pending_orders(self) -> tuple[PendingOrderSummary, ...]:
        try:
            payload = self._client.orderbook()
        except FyersApiError as exc:
            raise BrokerError(str(exc)) from exc
        if payload.get("s") != "ok":
            raise BrokerError(payload.get("message", "orderbook query failed"))
        rows = payload.get("orderBook")
        if not isinstance(rows, list):
            return ()
        pending: list[PendingOrderSummary] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            summary = parse_pending_order(row)
            if summary.state.is_working:
                pending.append(summary)
        return tuple(pending)

    def get_funds(self) -> BrokerFunds:
        received_at = self._clock.now_utc()
        try:
            payload = self._client.funds()
        except FyersApiError as exc:
            raise BrokerError(str(exc)) from exc
        if payload.get("s") != "ok":
            raise BrokerError(payload.get("message", "funds query failed"))
        return parse_funds(
            payload,
            account_id=self._config.account_id,
            as_of=received_at,
        )

    def preview_margin(self, request: MarginPreviewRequest) -> MarginPreviewResult:
        """Return broker margin preview; fail closed when unverified or errored."""
        funds = self.get_funds()
        if not request.legs:
            raise BrokerError("margin preview requires at least one leg")
        if not self._config.margin_preview_verified:
            return MarginPreviewResult(
                request_id=request.request_id,
                as_of=self._clock.now_utc(),
                margin_required=Money.zero(_INR),
                margin_available_after=funds.margin_available,
                confirmed=False,
            )

        legs = [
            _margin_leg_payload(leg, product_type=self._config.product_type)
            for leg in request.legs
        ]
        try:
            payload = self._client.multiorder_margin(legs)
        except FyersApiError:
            return MarginPreviewResult(
                request_id=request.request_id,
                as_of=self._clock.now_utc(),
                margin_required=Money.zero(_INR),
                margin_available_after=funds.margin_available,
                confirmed=False,
            )
        if payload.get("s") != "ok":
            return MarginPreviewResult(
                request_id=request.request_id,
                as_of=self._clock.now_utc(),
                margin_required=Money.zero(_INR),
                margin_available_after=funds.margin_available,
                confirmed=False,
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            return MarginPreviewResult(
                request_id=request.request_id,
                as_of=self._clock.now_utc(),
                margin_required=Money.zero(_INR),
                margin_available_after=funds.margin_available,
                confirmed=False,
            )
        margin_new = data.get("margin_new_order")
        margin_avail = data.get("margin_avail")
        if margin_new is None or margin_avail is None:
            return MarginPreviewResult(
                request_id=request.request_id,
                as_of=self._clock.now_utc(),
                margin_required=Money.zero(_INR),
                margin_available_after=funds.margin_available,
                confirmed=False,
            )
        required = Money.of(str(margin_new), _INR)
        available_after = Money.of(str(margin_avail), _INR)
        return MarginPreviewResult(
            request_id=request.request_id,
            as_of=self._clock.now_utc(),
            margin_required=required,
            margin_available_after=available_after,
            confirmed=True,
        )

    def _fetch_order_row(self, broker_order_id: str) -> dict[str, Any] | None:
        payload = self._client.orderbook(order_id=broker_order_id)
        if payload.get("s") != "ok":
            return None
        rows = payload.get("orderBook")
        if not isinstance(rows, list) or not rows:
            return None
        row = rows[0]
        return row if isinstance(row, dict) else None

    def _remember(self, event: OrderEvent) -> OrderEvent:
        self._orders_by_internal_id[event.identity.internal_order_id] = event
        self._orders_by_idempotency_key[event.identity.idempotency_key] = event
        return event

    def _terminal_event(
        self,
        request: BrokerSubmitRequest,
        *,
        state: OrderState,
        received_at: datetime,
        sent_at: datetime,
        raw_status: str,
        detail: str,
    ) -> OrderEvent:
        order = request.order
        reason_code = (
            ReasonCode.BROKER_REJECTED
            if state is OrderState.REJECTED
            else ReasonCode.ORDER_TIMEOUT
            if state is OrderState.UNKNOWN
            else None
        )
        return OrderEvent.model_validate(
            {
                "event_id": self._ids.new_id("EVT"),
                "identity": order.identity.model_dump(mode="python"),
                "command": order.command.model_dump(mode="python"),
                "state": state,
                "attempt_number": request.attempt_number,
                "sent_at": sent_at,
                "received_at": received_at,
                "broker_time": received_at,
                "raw_broker_status": raw_status,
                "reason_code": reason_code,
                "reason_detail": detail,
            }
        )


def _margin_leg_payload(
    leg: MarginPreviewLeg,
    *,
    product_type: str,
) -> dict[str, object]:
    return {
        "symbol": to_fyers_symbol(leg.contract),
        "qty": leg.quantity_contracts,
        "side": 1 if leg.side is Side.BUY else -1,
        "type": 1,
        "productType": product_type,
        "limitPrice": 0.0,
        "stopLoss": 0.0,
        "stopPrice": 0.0,
        "takeProfit": 0.0,
    }


def _int_code(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    return int(value)
