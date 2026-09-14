"""Trade manager and deterministic exits (L2-008).

Invariant 16: every open position maps to active protective coverage.
Invariant 17: stops never widen after entry.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import tests.factories as f
from trading.domain.clock import FrozenClock
from trading.domain.contracts import OrderEvent
from trading.domain.enums import OrderState, ReservationState, Side, TradeState
from trading.domain.ids import SequentialIdFactory
from trading.risk.reservation import CapitalReservationService
from trading.storage.trading_store import TradingStore
from trading.trade import ExitKind, TradeManager, build_exit_policy
from trading.trade.exits import ExitEngine, tighten_exit_policy

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def manager(clock: FrozenClock, id_factory: SequentialIdFactory) -> TradeManager:
    return TradeManager(clock=clock, id_factory=id_factory)


def _filled_entry_event(trade_id: str = "TRD-1") -> OrderEvent:
    return f.order_event(
        state=OrderState.FILLED,
        filled_quantity=75,
        average_fill_price=f.price("100.00"),
        identity=f.order_identity(trade_id=trade_id, broker_order_id="BRK-FILL-1"),
    )


def _partial_entry_event(trade_id: str = "TRD-1") -> OrderEvent:
    return f.order_event(
        state=OrderState.PARTIAL,
        filled_quantity=25,
        average_fill_price=f.price("100.00"),
        identity=f.order_identity(trade_id=trade_id, broker_order_id="BRK-PART-1"),
    )


class TestTradeLifecycle:
    def test_full_entry_requires_protective_coverage_before_open(
        self,
        manager: TradeManager,
    ) -> None:
        """Invariant 16: OPEN requires protective order references."""
        intent = f.intent()
        plan = f.order_plan()
        trade_id = manager.begin_entry(intent, plan)
        fill = _filled_entry_event(trade_id)

        manager.apply_order_event(fill, intent=intent)
        opening = manager.get_position(trade_id)
        assert opening is not None
        assert opening.state is TradeState.OPENING

        open_position = manager.register_protective_orders(trade_id, ("PROT-ORD-1",))
        assert open_position.state is TradeState.OPEN
        assert open_position.protective_order_ids == ("PROT-ORD-1",)
        assert open_position.opened_at == NOW

    def test_partial_entry_moves_to_repair_required(
        self,
        manager: TradeManager,
    ) -> None:
        """Invariant 15: partial entry routes to REPAIR_REQUIRED."""
        intent = f.intent()
        plan = f.order_plan()
        trade_id = manager.begin_entry(intent, plan)
        partial = _partial_entry_event(trade_id)

        repaired = manager.apply_order_event(partial, intent=intent)

        assert repaired.state is TradeState.REPAIR_REQUIRED
        assert repaired.legs[0].quantity_contracts == 25
        assert repaired.protective_order_ids


class TestExitEngine:
    def test_stop_exit_fires_on_bid_at_or_below_stop(self) -> None:
        entry = f.price("100.00")
        policy = build_exit_policy(
            f.exit_template(stop_distance_ticks=200),
            trade_id="TRD-1",
            policy_id="EXIT-POL-1",
            entry_price=entry,
            initialized_at=NOW,
        )
        position = f.position_state(
            exit_policy=policy,
            legs=(f.position_leg_state(average_entry_price=entry),),
        )
        feature = f.snapshot(market=f.quote(bid=f.price("89.95"), ask=f.price("90.00")))
        engine = ExitEngine()
        evaluation = engine.evaluate(position, feature, f.intent(), now=NOW)

        assert evaluation.kind is ExitKind.STOP
        assert evaluation.should_exit

    def test_target_exit_fires_on_bid_at_or_above_target(self) -> None:
        entry = f.price("100.00")
        policy = build_exit_policy(
            f.exit_template(stop_distance_ticks=200, target_distance_ticks=400),
            trade_id="TRD-1",
            policy_id="EXIT-POL-1",
            entry_price=entry,
            initialized_at=NOW,
        )
        position = f.position_state(
            exit_policy=policy,
            legs=(f.position_leg_state(average_entry_price=entry),),
        )
        feature = f.snapshot(
            market=f.quote(bid=f.price("120.00"), ask=f.price("120.05")),
        )
        engine = ExitEngine()
        evaluation = engine.evaluate(position, feature, f.intent(), now=NOW)

        assert evaluation.kind is ExitKind.TARGET
        assert evaluation.should_exit

    def test_time_exit_fires_at_scheduled_time(self) -> None:
        entry = f.price("100.00")
        policy = build_exit_policy(
            f.exit_template(
                stop_distance_ticks=200,
                time_exit=NOW - timedelta(minutes=1),
            ),
            trade_id="TRD-1",
            policy_id="EXIT-POL-1",
            entry_price=entry,
            initialized_at=NOW - timedelta(hours=1),
        )
        position = f.position_state(
            exit_policy=policy,
            legs=(f.position_leg_state(average_entry_price=entry),),
        )
        feature = f.snapshot(
            market=f.quote(bid=f.price("101.00"), ask=f.price("101.05")),
        )
        engine = ExitEngine()
        evaluation = engine.evaluate(position, feature, f.intent(), now=NOW)

        assert evaluation.kind is ExitKind.TIME
        assert evaluation.should_exit

    def test_invariant_17_trailing_only_tightens_stop(self) -> None:
        """Invariant 17: trailing adjustment never widens the stop."""
        entry = f.price("100.00")
        template = f.exit_template(
            stop_distance_ticks=200,
            trailing_activation_ticks=50,
            trailing_distance_ticks=80,
        )
        policy = build_exit_policy(
            template,
            trade_id="TRD-1",
            policy_id="EXIT-POL-1",
            entry_price=entry,
            initialized_at=NOW,
        )
        initial_stop = policy.stop_price
        assert initial_stop is not None

        tightened = tighten_exit_policy(
            policy,
            template,
            entry_price=entry,
            monitor_price=f.price("103.00"),
        )
        assert tightened is not None
        assert tightened.stop_price is not None
        assert tightened.stop_price.value > initial_stop.value
        assert (
            tightened.current_stop_distance_ticks
            <= tightened.initial_stop_distance_ticks
        )

        with pytest.raises(ValidationError, match="may only tighten"):
            f.exit_policy(
                initial_stop_distance_ticks=200,
                current_stop_distance_ticks=201,
            )

    def test_exit_fill_closes_trade_and_releases_reservation(
        self,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
        tmp_path: Path,
    ) -> None:
        store = TradingStore.open(tmp_path / "trade.sqlite", clock=clock)
        reservations = CapitalReservationService(
            store,
            clock=clock,
            id_factory=id_factory,
        )
        manager_with_reservations = TradeManager(
            clock=clock,
            id_factory=id_factory,
            reservation_service=reservations,
        )
        reserved = reservations.try_reserve(
            intent_id="INT-1",
            strategy_id="positional_index_options_poc",
            amount=f.money("10000"),
            margin_available=f.money("700000"),
            risk_decision_id="DEC-1",
        )
        committed = reservations.commit(reserved.reservation_id)
        entry = f.price("100.00")
        policy = build_exit_policy(
            f.exit_template(stop_distance_ticks=200),
            trade_id="TRD-1",
            policy_id="EXIT-POL-1",
            entry_price=entry,
            initialized_at=NOW,
        )
        manager_with_reservations._positions["TRD-1"] = f.position_state(
            trade_id="TRD-1",
            state=TradeState.EXIT_PENDING,
            exit_policy=policy,
            legs=(f.position_leg_state(average_entry_price=entry),),
        )
        exit_fill = f.order_event(
            state=OrderState.FILLED,
            filled_quantity=75,
            average_fill_price=f.price("89.95"),
            identity=f.order_identity(
                trade_id="TRD-1",
                broker_order_id="BRK-EXIT-1",
            ),
            command=f.order_command(side=Side.SELL),
        )
        closed = manager_with_reservations.apply_exit_order_event(
            exit_fill,
            capital_reservation_id=committed.reservation_id,
        )
        assert closed.state is TradeState.CLOSED
        released = store.get_reservation(committed.reservation_id)
        assert released is not None
        assert released.state is ReservationState.RELEASED
        store.close()

    def test_manager_applies_exit_evaluation_to_exit_pending(
        self,
        manager: TradeManager,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        entry = f.price("100.00")
        policy = build_exit_policy(
            f.exit_template(stop_distance_ticks=200),
            trade_id="TRD-1",
            policy_id="EXIT-POL-1",
            entry_price=entry,
            initialized_at=NOW,
        )
        manager._positions["TRD-1"] = f.position_state(
            trade_id="TRD-1",
            state=TradeState.OPEN,
            exit_policy=policy,
            legs=(f.position_leg_state(average_entry_price=entry),),
        )
        feature = f.snapshot(market=f.quote(bid=f.price("89.95"), ask=f.price("90.00")))
        evaluation = manager.evaluate_exit("TRD-1", feature, f.intent())
        updated = manager.apply_exit_evaluation("TRD-1", evaluation)

        assert updated.state is TradeState.EXIT_PENDING
        assert evaluation.kind is ExitKind.STOP
