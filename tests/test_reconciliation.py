"""Portfolio snapshot and reconciliation (L2-004).

Invariant 5: broker-reported funds, positions and orders are external truth.
Invariant 6: inconsistent state blocks new entries.
Invariant 9: startup begins in RECOVERY; entries wait for reconciliation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import tests.factories as f
from trading.broker.paper import PaperBroker
from trading.broker.ports import BrokerSubmitRequest
from trading.domain.clock import FrozenClock
from trading.domain.contracts.reconciliation import ReconciliationEvent
from trading.domain.enums import (
    DifferenceClass,
    OrderState,
    Severity,
    Side,
    SystemState,
)
from trading.domain.ids import SequentialIdFactory
from trading.portfolio import PortfolioReconciler, build_broker_snapshot
from trading.storage import TradingEventType, TradingStore

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
ACCOUNT_ID = "ACC-PAPER-1"


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def broker(clock: FrozenClock, id_factory: SequentialIdFactory) -> PaperBroker:
    return PaperBroker.from_fixtures(FIXTURES, clock=clock, id_factory=id_factory)


@pytest.fixture
def store(clock: FrozenClock, tmp_path: Path) -> TradingStore:
    return TradingStore.open(tmp_path / "trading.db", clock=clock)


@pytest.fixture
def reconciler(
    broker: PaperBroker,
    store: TradingStore,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> PortfolioReconciler:
    return PortfolioReconciler(
        broker,
        store,
        clock=clock,
        id_factory=id_factory,
        versions=f.versions(),
    )


def _submit_request(**overrides: object) -> BrokerSubmitRequest:
    return BrokerSubmitRequest.model_validate(
        {
            "account_id": ACCOUNT_ID,
            "strategy_id": "positional_index_options_poc",
            "order": f.planned_order(),
            **overrides,
        }
    )


class TestBrokerSnapshot:
    def test_build_broker_snapshot_reflects_broker_funds(
        self,
        broker: PaperBroker,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Invariant 5: portfolio truth is built from broker queries."""
        snapshot = build_broker_snapshot(
            broker,
            account_id=ACCOUNT_ID,
            versions=f.versions(),
            id_factory=id_factory,
            reserved_capital=f.money("0"),
        )
        assert snapshot.account_id == ACCOUNT_ID
        assert snapshot.exposure.equity == f.money("700000")
        assert snapshot.exposure.margin_available == f.money("700000")
        assert snapshot.positions == ()


class TestBootReconcile:
    def test_clean_boot_opens_entries(
        self,
        reconciler: PortfolioReconciler,
        store: TradingStore,
    ) -> None:
        """Invariant 9: startup reconciles before entries are permitted."""
        assert store.get_system_state() == (SystemState.STARTING, None)
        outcome = reconciler.boot_reconcile(ACCOUNT_ID)

        assert outcome.result.prior_system_state is SystemState.STARTING
        assert outcome.result.resulting_system_state is SystemState.READY
        assert outcome.result.entries_blocked is False
        assert outcome.portfolio_view.entries_permitted is True
        assert store.get_system_state()[0] is SystemState.READY

    def test_boot_reconcile_persists_audit_events(
        self,
        reconciler: PortfolioReconciler,
        store: TradingStore,
    ) -> None:
        """Invariant 25: every comparison produces durable evidence."""
        outcome = reconciler.boot_reconcile(ACCOUNT_ID)
        stored = store.read_events()
        assert len(stored) >= 2
        events: list[ReconciliationEvent] = []
        for row in stored:
            event = row.deserialize()
            assert isinstance(event, ReconciliationEvent)
            events.append(event)
        assert any(event.difference_class is DifferenceClass.NONE for event in events)
        assert store.get_system_state()[1] == outcome.result.result_id

    def test_local_working_order_absent_at_broker_blocks_entries(
        self,
        reconciler: PortfolioReconciler,
        store: TradingStore,
    ) -> None:
        """Invariant 6: inconsistent local/broker order state blocks entries."""
        store.append(
            TradingEventType.ORDER_EVENT,
            f.order_event(
                state=OrderState.SUBMITTING,
                acknowledged_quantity=0,
                identity=f.order_identity(broker_order_id=None),
            ),
            event_id="EVT-LOCAL-1",
        )
        outcome = reconciler.boot_reconcile(ACCOUNT_ID)

        assert outcome.result.entries_blocked is True
        assert outcome.result.resulting_system_state is SystemState.RECOVERY
        assert outcome.portfolio_view.entries_permitted is False
        assert any(
            event.difference_class is DifferenceClass.LOCAL_ORDER_ABSENT_AT_BROKER
            for event in outcome.result.events
        )

    def test_unexpected_broker_position_blocks_entries(
        self,
        broker: PaperBroker,
        reconciler: PortfolioReconciler,
    ) -> None:
        """Invariant 5: broker positions unknown locally freeze new entries."""
        broker.submit(_submit_request())
        outcome = reconciler.boot_reconcile(ACCOUNT_ID)

        assert outcome.result.entries_blocked is True
        assert any(
            event.difference_class is DifferenceClass.UNEXPECTED_BROKER_STATE
            and event.severity is Severity.CRITICAL
            for event in outcome.result.events
        )
        assert outcome.broker_snapshot.positions[0].trade_id == "TRD-1"

    def test_closed_trade_reconciles_without_open_broker_position(
        self,
        broker: PaperBroker,
        reconciler: PortfolioReconciler,
        store: TradingStore,
    ) -> None:
        """Net-zero local fills must not require an open broker position."""
        entry = broker.submit(_submit_request())
        exit_order = f.planned_order(
            identity=f.order_identity(
                internal_order_id="ORD-EXIT-1",
                trade_id=entry.identity.trade_id,
                idempotency_key="exit-key-1",
            ),
            command=f.order_command(side=Side.SELL, limit_price=f.price("118.00")),
        )
        exit_fill = broker.submit(
            _submit_request(order=exit_order, attempt_number=1),
        )
        store.append(
            TradingEventType.ORDER_EVENT,
            entry,
            event_id=entry.event_id,
            idempotency_key=entry.identity.idempotency_key,
        )
        store.append(
            TradingEventType.ORDER_EVENT,
            exit_fill,
            event_id=exit_fill.event_id,
            idempotency_key=exit_fill.identity.idempotency_key,
        )
        outcome = reconciler.boot_reconcile(ACCOUNT_ID)

        assert outcome.result.entries_blocked is False
        assert broker.get_positions() == ()

    def test_matched_broker_and_local_positions_open_entries(
        self,
        broker: PaperBroker,
        reconciler: PortfolioReconciler,
        store: TradingStore,
    ) -> None:
        """A broker fill recorded locally reconciles cleanly."""
        filled = broker.submit(_submit_request())
        store.append(
            TradingEventType.ORDER_EVENT,
            filled,
            event_id=filled.event_id,
            idempotency_key=filled.identity.idempotency_key,
        )
        outcome = reconciler.boot_reconcile(ACCOUNT_ID)

        assert outcome.result.entries_blocked is False
        assert outcome.result.resulting_system_state is SystemState.READY
        assert outcome.broker_snapshot.exposure.open_trade_count == 1
