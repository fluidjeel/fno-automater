"""OMS core: durable, idempotent order submission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from trading.broker.ports import (
    BrokerPort,
    BrokerSubmitRequest,
    BrokerSubmitTimeoutError,
)
from trading.domain.clock import Clock
from trading.domain.contracts.order import OrderEvent
from trading.domain.contracts.order_plan import OrderPlan, PlannedOrder
from trading.domain.enums import OrderState, ReasonCode, Trigger
from trading.domain.ids import IdFactory
from trading.domain.state import ORDER_MACHINE, IllegalTransitionError
from trading.oms.rate_limit import OrderRateLimiter
from trading.storage.trading_store import (
    DuplicateIdempotencyKeyError,
    TradingEventType,
    TradingStore,
)

__all__ = [
    "OmsEngine",
    "OmsError",
    "OrderFrozenError",
    "OrderPlanExpiredError",
    "SubmitResult",
]


_SUBMIT_STOP_STATES = frozenset(
    {OrderState.REJECTED, OrderState.CANCELLED, OrderState.UNKNOWN}
)


class OmsError(Exception):
    """Base error for OMS failures."""


class OrderPlanExpiredError(OmsError):
    """Raised when submission is attempted after plan expiry."""

    reason_code = ReasonCode.DECISION_EXPIRED


class OrderFrozenError(OmsError):
    """Invariant 13: UNKNOWN outcome blocks replacement until reconciliation."""

    reason_code = ReasonCode.RECONCILIATION_UNRESOLVED

    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(
            f"order {idempotency_key} is UNKNOWN; submit blocked until reconciliation"
        )


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Outcome of submitting every leg in an OrderPlan."""

    plan_id: str
    events: tuple[OrderEvent, ...]


class OmsEngine:
    """Drive order submission with durable writes and idempotent retries."""

    def __init__(
        self,
        store: TradingStore,
        broker: BrokerPort,
        *,
        clock: Clock,
        id_factory: IdFactory,
        rate_limiter: OrderRateLimiter,
        durable_write_required_before_submit: bool = True,
    ) -> None:
        self._store = store
        self._broker = broker
        self._clock = clock
        self._ids = id_factory
        self._rate_limiter = rate_limiter
        self._durable_write_required = durable_write_required_before_submit
        self._latest_by_key: dict[str, OrderEvent] = {}
        self._replay_store()

    def submit_plan(
        self,
        plan: OrderPlan,
        *,
        strategy_id: str,
        account_id: str,
    ) -> SubmitResult:
        """Submit each planned leg once with stable idempotency keys."""
        now = self._clock.now_utc()
        if not plan.is_valid_at(now):
            raise OrderPlanExpiredError(
                f"order plan {plan.plan_id} expired at {plan.expires_at}"
            )

        events: list[OrderEvent] = []
        for order in plan.orders:
            event = self._submit_one(
                order,
                strategy_id=strategy_id,
                account_id=account_id,
            )
            events.append(event)
            if event.state in _SUBMIT_STOP_STATES:
                # Plans are ordered so that every prefix is a position that is
                # safe to hold: entries buy protection before selling the legs
                # it covers, exits buy back short liabilities before releasing
                # the longs. Submitting past a failed leg is what turns a
                # defined-risk structure into an uncovered short, so stop here
                # and leave the covered prefix for the caller to reconcile.
                break
        return SubmitResult(plan_id=plan.plan_id, events=tuple(events))

    def latest_event(self, idempotency_key: str) -> OrderEvent | None:
        """Return the latest known event for one logical order."""
        return self._latest_by_key.get(idempotency_key)

    def _submit_one(
        self,
        planned: PlannedOrder,
        *,
        strategy_id: str,
        account_id: str,
    ) -> OrderEvent:
        key = planned.identity.idempotency_key
        existing = self._latest_by_key.get(key)
        if existing is not None:
            if existing.state is OrderState.UNKNOWN:
                raise OrderFrozenError(key)
            return existing

        self._rate_limiter.check_and_consume()
        now = self._clock.now_utc()
        created = self._build_event(
            planned,
            state=OrderState.CREATED,
            attempt_number=1,
            received_at=now,
        )
        self._persist(created, register_idempotency=True)

        submitting = self._transition(
            created,
            OrderState.SUBMITTING,
            trigger=Trigger.LOCAL_COMMAND,
            sent_at=now,
        )
        self._persist(submitting, register_idempotency=False)

        if self._durable_write_required and not self._has_persisted_state(
            key, OrderState.CREATED
        ):
            raise OmsError("durable OrderEvent(CREATED) missing before broker submit")

        request = BrokerSubmitRequest(
            account_id=account_id,
            strategy_id=strategy_id,
            order=planned,
            attempt_number=submitting.attempt_number,
        )
        try:
            broker_event = self._broker.submit(request)
        except BrokerSubmitTimeoutError:
            unknown = self._build_unknown_timeout(submitting, now=now)
            self._persist(unknown, register_idempotency=False)
            self._latest_by_key[key] = unknown
            raise OrderFrozenError(key) from None

        final = self._adopt_broker_event(submitting, broker_event)
        self._persist(final, register_idempotency=False)
        return final

    def _adopt_broker_event(
        self,
        local: OrderEvent,
        broker_event: OrderEvent,
    ) -> OrderEvent:
        try:
            ORDER_MACHINE.transition(
                local.state,
                broker_event.state,
                trigger=Trigger.BROKER_EVENT,
                at=broker_event.received_at,
            )
        except IllegalTransitionError as exc:
            raise OmsError(exc.record.detail) from exc
        return broker_event.model_copy(
            update={"event_id": self._ids.new_id("EVT")},
        )

    def _build_unknown_timeout(
        self,
        submitting: OrderEvent,
        *,
        now: datetime,
    ) -> OrderEvent:
        internal_id = submitting.identity.internal_order_id
        identity = submitting.identity.model_copy(
            update={"broker_order_id": f"UNRESOLVED-{internal_id}"},
        )
        return OrderEvent.model_validate(
            {
                **submitting.model_dump(mode="python"),
                "event_id": self._ids.new_id("EVT"),
                "identity": identity.model_dump(mode="python"),
                "state": OrderState.UNKNOWN,
                "filled_quantity": 0,
                "acknowledged_quantity": 0,
                "average_fill_price": None,
                "sent_at": now,
                "received_at": now,
                "reason_code": ReasonCode.ORDER_TIMEOUT,
                "reason_detail": "broker submit timed out before acknowledgement",
                "raw_broker_status": "UNKNOWN",
            }
        )

    def _transition(
        self,
        current: OrderEvent,
        target: OrderState,
        *,
        trigger: Trigger,
        sent_at: datetime,
    ) -> OrderEvent:
        ORDER_MACHINE.transition(
            current.state,
            target,
            trigger=trigger,
            at=sent_at,
        )
        return current.model_copy(
            update={
                "event_id": self._ids.new_id("EVT"),
                "state": target,
                "sent_at": sent_at,
                "received_at": sent_at,
            },
        )

    def _build_event(
        self,
        planned: PlannedOrder,
        *,
        state: OrderState,
        attempt_number: int,
        received_at: datetime,
        sent_at: datetime | None = None,
    ) -> OrderEvent:
        payload: dict[str, object] = {
            "event_id": self._ids.new_id("EVT"),
            "identity": planned.identity.model_dump(mode="python"),
            "command": planned.command.model_dump(mode="python"),
            "state": state,
            "attempt_number": attempt_number,
            "received_at": received_at,
        }
        if sent_at is not None:
            payload["sent_at"] = sent_at
        return OrderEvent.model_validate(payload)

    def _persist(self, event: OrderEvent, *, register_idempotency: bool) -> None:
        key = event.identity.idempotency_key if register_idempotency else None
        try:
            self._store.append(
                TradingEventType.ORDER_EVENT,
                event,
                event_id=event.event_id,
                idempotency_key=key,
            )
        except DuplicateIdempotencyKeyError:
            self._replay_store()
            return
        self._latest_by_key[event.identity.idempotency_key] = event

    def _has_persisted_state(self, idempotency_key: str, state: OrderState) -> bool:
        for stored in self._store.read_events():
            if stored.event_type is not TradingEventType.ORDER_EVENT:
                continue
            event = stored.deserialize()
            if not isinstance(event, OrderEvent):
                continue
            if (
                event.identity.idempotency_key == idempotency_key
                and event.state is state
            ):
                return True
        return False

    def _replay_store(self) -> None:
        for stored in self._store.read_events():
            if stored.event_type is not TradingEventType.ORDER_EVENT:
                continue
            event = stored.deserialize()
            if not isinstance(event, OrderEvent):
                continue
            key = event.identity.idempotency_key
            current = self._latest_by_key.get(key)
            if current is None or event.received_at >= current.received_at:
                self._latest_by_key[key] = event
