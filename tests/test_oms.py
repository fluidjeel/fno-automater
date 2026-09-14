"""OrderPlan builder and OMS core (L2-007).

Invariant 11: one logical order keeps one idempotency key across retries.
Invariant 13: UNKNOWN submit outcome blocks replacement until reconciliation.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import tests.factories as f
from trading.broker.paper import PaperBroker
from trading.broker.ports import (
    BrokerFunds,
    BrokerPort,
    BrokerSubmitRequest,
    BrokerSubmitTimeoutError,
)
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    FeatureSnapshot,
    OrderEvent,
    PendingOrderSummary,
    PositionRecord,
    RiskDecision,
    TradeIntent,
)
from trading.domain.enums import OrderState, ReasonCode
from trading.domain.ids import SequentialIdFactory
from trading.oms import (
    OmsEngine,
    OrderFrozenError,
    OrderPlanPlanner,
    OrderPlanRequest,
    OrderRateLimiter,
    PlannerError,
    RateLimitExceededError,
)
from trading.storage.trading_store import TradingEventType, TradingStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "trading.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


@pytest.fixture
def broker(clock: FrozenClock, id_factory: SequentialIdFactory) -> PaperBroker:
    return PaperBroker.from_fixtures(FIXTURES, clock=clock, id_factory=id_factory)


def _approved_request(
    *,
    intent: TradeIntent | None = None,
    decision: RiskDecision | None = None,
    feature_snapshot: FeatureSnapshot | None = None,
    account_id: str = "ACC-PAPER-1",
) -> OrderPlanRequest:
    snap = feature_snapshot or f.snapshot()
    trade_intent = intent or f.intent(snapshot_id=snap.snapshot_id)
    risk = decision or f.risk_decision(intent_id=trade_intent.intent_id)
    return OrderPlanRequest(
        intent=trade_intent,
        decision=risk,
        feature_snapshot=snap,
        account_id=account_id,
    )


def _engine(
    store: TradingStore,
    broker: BrokerPort,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
    *,
    max_per_second: int | None = None,
) -> OmsEngine:
    return OmsEngine(
        store,
        broker,
        clock=clock,
        id_factory=id_factory,
        rate_limiter=OrderRateLimiter(
            clock=clock,
            max_per_second=max_per_second,
        ),
        durable_write_required_before_submit=True,
    )


class TestOrderPlanPlanner:
    def test_builds_limit_order_at_ask_with_protective_stop(self) -> None:
        """Executable prices and protective stubs come from snapshot and template."""
        planner = OrderPlanPlanner(
            clock=FrozenClock(NOW),
            id_factory=SequentialIdFactory(NOW),
        )
        plan = planner.build(_approved_request())

        assert len(plan.orders) == 1
        order = plan.orders[0]
        assert order.command.limit_price == f.price("100.05")
        assert order.command.quantity_contracts == 75
        assert len(plan.protective_orders) == 1
        stub = plan.protective_orders[0]
        assert stub.trigger_price == f.price("90.05")

    def test_rejects_expired_risk_decision(self) -> None:
        planner = OrderPlanPlanner(
            clock=FrozenClock(NOW),
            id_factory=SequentialIdFactory(NOW),
        )
        with pytest.raises(PlannerError, match="expired") as exc:
            planner.build(
                _approved_request(
                    decision=f.risk_decision(
                        decided_at=NOW - timedelta(minutes=10),
                        expires_at=NOW - timedelta(minutes=5),
                        intent_id="INT-1",
                    )
                )
            )
        assert exc.value.reason_code is ReasonCode.DECISION_EXPIRED


class TestOmsSubmit:
    def test_submit_fills_through_paper_broker(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        planner = OrderPlanPlanner(clock=clock, id_factory=id_factory)
        plan = planner.build(_approved_request())
        engine = _engine(store, broker, clock, id_factory)

        result = engine.submit_plan(
            plan,
            strategy_id="positional_index_options_poc",
            account_id="ACC-PAPER-1",
        )

        assert len(result.events) == 1
        assert result.events[0].state is OrderState.FILLED
        assert broker.get_positions()

    def test_invariant_11_idempotent_submit_reuses_one_key(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Invariant 11: duplicate submit returns the same logical order."""
        planner = OrderPlanPlanner(clock=clock, id_factory=id_factory)
        plan = planner.build(_approved_request())
        engine = _engine(store, broker, clock, id_factory)

        first = engine.submit_plan(
            plan,
            strategy_id="positional_index_options_poc",
            account_id="ACC-PAPER-1",
        )
        second = engine.submit_plan(
            plan,
            strategy_id="positional_index_options_poc",
            account_id="ACC-PAPER-1",
        )

        assert (
            first.events[0].identity.idempotency_key
            == second.events[0].identity.idempotency_key
        )
        assert first.events[0].identity.internal_order_id == (
            second.events[0].identity.internal_order_id
        )
        assert len(broker.list_orders()) == 1

    def test_durable_write_before_broker_submit(
        self,
        store: TradingStore,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """OrderEvent(CREATED) is persisted before BrokerPort.submit."""
        planner = OrderPlanPlanner(clock=clock, id_factory=id_factory)
        plan = planner.build(_approved_request())
        recording = _RecordingBroker(
            PaperBroker.from_fixtures(FIXTURES, clock=clock, id_factory=id_factory),
            store=store,
        )
        engine = _engine(store, recording, clock, id_factory)
        engine.submit_plan(
            plan,
            strategy_id="positional_index_options_poc",
            account_id="ACC-PAPER-1",
        )

        assert recording.submit_called
        created_before_submit = any(
            event.state is OrderState.CREATED
            for event in recording.events_before_submit
        )
        assert created_before_submit

    def test_invariant_13_unknown_freezes_replacement(
        self,
        store: TradingStore,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Invariant 13: UNKNOWN blocks a blind resubmit."""
        planner = OrderPlanPlanner(clock=clock, id_factory=id_factory)
        plan = planner.build(_approved_request())
        timeout_broker = _TimeoutBroker()
        engine = _engine(store, timeout_broker, clock, id_factory)

        with pytest.raises(OrderFrozenError):
            engine.submit_plan(
                plan,
                strategy_id="positional_index_options_poc",
                account_id="ACC-PAPER-1",
            )

        with pytest.raises(OrderFrozenError):
            engine.submit_plan(
                plan,
                strategy_id="positional_index_options_poc",
                account_id="ACC-PAPER-1",
            )

        key = plan.orders[0].identity.idempotency_key
        latest = engine.latest_event(key)
        assert latest is not None
        assert latest.state is OrderState.UNKNOWN

    def test_partial_fill_is_recorded(
        self,
        store: TradingStore,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Broker-confirmed partial fills persist without implying completion."""
        planner = OrderPlanPlanner(clock=clock, id_factory=id_factory)
        plan = planner.build(_approved_request())
        partial_broker = _PartialFillBroker(clock=clock, id_factory=id_factory)
        engine = _engine(store, partial_broker, clock, id_factory)

        result = engine.submit_plan(
            plan,
            strategy_id="positional_index_options_poc",
            account_id="ACC-PAPER-1",
        )

        assert result.events[0].state is OrderState.PARTIAL
        assert result.events[0].filled_quantity == 25
        assert result.events[0].outstanding_quantity == 50

    def test_rate_limit_blocks_burst_submits(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        planner = OrderPlanPlanner(clock=clock, id_factory=id_factory)
        engine = _engine(store, broker, clock, id_factory, max_per_second=1)

        first_plan = planner.build(
            _approved_request(intent=f.intent(intent_id="INT-A"))
        )
        second_plan = planner.build(
            _approved_request(intent=f.intent(intent_id="INT-B"))
        )
        engine.submit_plan(
            first_plan,
            strategy_id="positional_index_options_poc",
            account_id="ACC-PAPER-1",
        )
        with pytest.raises(RateLimitExceededError):
            engine.submit_plan(
                second_plan,
                strategy_id="positional_index_options_poc",
                account_id="ACC-PAPER-1",
            )


@dataclass
class _RecordingBroker:
    """Broker wrapper that records store state at submit time."""

    inner: BrokerPort
    store: TradingStore | None = None
    submit_called: bool = False
    events_before_submit: tuple[OrderEvent, ...] = ()

    def submit(self, request: BrokerSubmitRequest) -> OrderEvent:
        if self.store is not None:
            events: list[OrderEvent] = []
            for stored in self.store.read_events():
                if stored.event_type is not TradingEventType.ORDER_EVENT:
                    continue
                event = stored.deserialize()
                assert isinstance(event, OrderEvent)
                events.append(event)
            self.events_before_submit = tuple(events)
        self.submit_called = True
        return self.inner.submit(request)

    def cancel(self, internal_order_id: str) -> OrderEvent:
        return self.inner.cancel(internal_order_id)

    def get_order(self, internal_order_id: str) -> OrderEvent | None:
        return self.inner.get_order(internal_order_id)

    def list_orders(self) -> tuple[OrderEvent, ...]:
        return self.inner.list_orders()

    def get_positions(self) -> tuple[PositionRecord, ...]:
        return self.inner.get_positions()

    def get_pending_orders(self) -> tuple[PendingOrderSummary, ...]:
        return self.inner.get_pending_orders()

    def get_funds(self) -> BrokerFunds:
        return self.inner.get_funds()


class _TimeoutBroker:
    def submit(self, request: BrokerSubmitRequest) -> OrderEvent:
        raise BrokerSubmitTimeoutError(request.order.identity.idempotency_key)

    def cancel(self, internal_order_id: str) -> OrderEvent:
        raise NotImplementedError

    def get_order(self, internal_order_id: str) -> OrderEvent | None:
        return None

    def list_orders(self) -> tuple[OrderEvent, ...]:
        return ()

    def get_positions(self) -> tuple[PositionRecord, ...]:
        return ()

    def get_pending_orders(self) -> tuple[PendingOrderSummary, ...]:
        return ()

    def get_funds(self) -> BrokerFunds:
        raise NotImplementedError


@dataclass
class _PartialFillBroker:
    clock: FrozenClock
    id_factory: SequentialIdFactory
    _events: dict[str, OrderEvent] = field(default_factory=dict)

    def submit(self, request: BrokerSubmitRequest) -> OrderEvent:
        order = request.order
        now = self.clock.now_utc()
        broker_order_id = self.id_factory.new_id("PBRK")
        event = OrderEvent.model_validate(
            {
                "event_id": self.id_factory.new_id("EVT"),
                "identity": {
                    **order.identity.model_dump(mode="python"),
                    "broker_order_id": broker_order_id,
                },
                "command": order.command.model_dump(mode="python"),
                "state": OrderState.PARTIAL,
                "attempt_number": request.attempt_number,
                "acknowledged_quantity": order.command.quantity_contracts,
                "filled_quantity": 25,
                "average_fill_price": order.command.limit_price,
                "sent_at": now,
                "received_at": now,
                "broker_time": now,
                "raw_broker_status": "PARTIAL",
            }
        )
        self._events[order.identity.internal_order_id] = event
        return event

    def cancel(self, internal_order_id: str) -> OrderEvent:
        raise NotImplementedError

    def get_order(self, internal_order_id: str) -> OrderEvent | None:
        return self._events.get(internal_order_id)

    def list_orders(self) -> tuple[OrderEvent, ...]:
        return tuple(self._events.values())

    def get_positions(self) -> tuple[PositionRecord, ...]:
        return ()

    def get_pending_orders(self) -> tuple[PendingOrderSummary, ...]:
        return ()

    def get_funds(self) -> BrokerFunds:
        raise NotImplementedError
