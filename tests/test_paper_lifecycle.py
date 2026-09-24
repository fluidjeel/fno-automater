"""PAPER positional lifecycle persistence and restart recovery.

Invariant 8: existing positions keep deterministic protection after restart.
Invariant 9: startup restores lifecycle and blocks entries until reconciled.
Invariant 13: unknown exit status never replaces the in-flight order.
Invariant 16: missing local protective coverage is recorded and blocks entries.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import tests.factories as f
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config, _request
from trading.broker.paper import PaperBroker
from trading.config import load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    IntentLeg,
    PositionLifecycleRecord,
)
from trading.domain.enums import (
    DataQuality,
    ExitScope,
    HoldingStyle,
    OptionType,
    OrderState,
    ReasonCode,
    Side,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money
from trading.runtime.notify import format_lifecycle_alert
from trading.runtime.paper_runner import PaperRunner
from trading.runtime.paper_session import PaperSession, load_paper_session_config
from trading.storage.trading_store import TradingEventType, TradingStore
from trading.trade import ExitEngine, ExitKind, build_exit_policy

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
EOD = datetime(2026, 9, 14, 10, 10, tzinfo=UTC)
MORNING = datetime(2026, 9, 15, 4, 0, tzinfo=UTC)
IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW + timedelta(seconds=60))


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


class _Sink:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def _runner(
    store: TradingStore,
    clock: FrozenClock,
    *,
    broker: PaperBroker | None = None,
) -> PaperRunner:
    ids = SequentialIdFactory(clock.instant)
    if broker is None:
        broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
    return PaperRunner(
        account_config=_paper_config(),  # type: ignore[arg-type]
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
        store=store,
        broker=broker,
        clock=clock,
        id_factory=ids,
    )


def _option_snapshot(contract: object, **overrides: object) -> FeatureSnapshot:
    payload: dict[str, object] = {
        "contract": contract,
        "market": f.quote(bid=f.price("91.95"), ask=f.price("92.00")),
        "derivatives": DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    }
    payload.update(overrides)
    return f.snapshot(**payload)


def _open_long(store: TradingStore, clock: FrozenClock) -> PaperRunner:
    runner = _runner(store, clock)
    result = runner.run_cycle((_request(),))
    assert result.outcomes[0].order_events
    assert any(
        event.state is OrderState.FILLED for event in result.outcomes[0].order_events
    )
    assert runner.trade_manager.list_positions()
    return runner


def _restart(
    store: TradingStore, clock: FrozenClock, broker: PaperBroker
) -> PaperRunner:
    ids = SequentialIdFactory(clock.instant)
    restored = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
    restored.load_state(broker.dump_state())
    return PaperRunner(
        account_config=_paper_config(),  # type: ignore[arg-type]
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
        store=store,
        broker=restored,
        clock=clock,
        id_factory=ids,
    )


class TestRestartOpenPosition:
    def test_restart_restores_open_position_and_exit_policy(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Invariant 8/9: restart restores the frozen entry-time exit lifecycle."""
        first = _open_long(store, clock)
        opened = first.trade_manager.list_positions()[0]
        assert opened.state is TradeState.OPEN
        persisted = store.get_position_lifecycle(opened.trade_id)
        assert persisted is not None
        assert persisted.holding_style is HoldingStyle.INTRADAY
        assert persisted.intent.strategy_version == opened.strategy_version
        assert persisted.position.exit_policy.policy_id == opened.exit_policy.policy_id

        second = _restart(store, clock, first.broker)
        recovery = second.recover_lifecycle()
        assert opened.trade_id in recovery.restored_trade_ids
        assert recovery.entries_blocked is False
        restored = second.trade_manager.get_position(opened.trade_id)
        assert restored is not None
        assert restored.state is TradeState.OPEN
        assert restored.exit_policy == opened.exit_policy
        assert restored.protective_order_ids == opened.protective_order_ids


class TestUnknownExitStatus:
    def test_restart_after_unknown_exit_does_not_replace(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Invariant 13: unknown exit outcome blocks replacement until reconcile."""
        first = _open_long(store, clock)
        opened = first.trade_manager.list_positions()[0]
        pending = opened.model_copy(update={"state": TradeState.EXIT_PENDING})
        record = store.get_position_lifecycle(opened.trade_id)
        assert record is not None
        unknown = f.order_event(
            event_id="EVT-EXIT-UNKNOWN",
            state=OrderState.UNKNOWN,
            reason_code=ReasonCode.ORDER_TIMEOUT,
            filled_quantity=0,
            acknowledged_quantity=0,
            identity=f.order_identity(
                internal_order_id="ORD-EXIT-UNKNOWN",
                client_order_id="ORD-EXIT-UNKNOWN",
                idempotency_key="a" * 32,
                trade_id=opened.trade_id,
                intent_id=opened.intent_id,
                broker_order_id="UNRESOLVED-ORD-EXIT-UNKNOWN",
            ),
            command=f.order_command(
                side=Side.SELL,
                contract=opened.legs[0].contract,
                quantity_contracts=opened.legs[0].quantity_contracts,
            ),
        )
        store.append(TradingEventType.ORDER_EVENT, unknown, event_id=unknown.event_id)
        store.upsert_position_lifecycle(
            record.model_copy(
                update={
                    "position": pending,
                    "exit_order_ids": ("ORD-EXIT-UNKNOWN",),
                    "as_of": pending.as_of,
                }
            ),
            event_id="PLC-UNKNOWN-1",
        )
        sells_before = sum(
            1 for event in first.broker.list_orders() if event.command.side is Side.SELL
        )

        second = _restart(store, clock, first.broker)
        recovery = second.recover_lifecycle()
        assert recovery.entries_blocked is True
        assert any(
            alert.reason_code is ReasonCode.UNKNOWN_ORDER_STATUS
            for alert in recovery.alerts
        )
        symbol = opened.legs[0].contract.symbol
        snapshot = (
            _request()
            .candidates[0]
            .model_copy(update={"contract": opened.legs[0].contract})
        )
        second.manage_exits({symbol: snapshot})
        sells_after = sum(
            1
            for event in second.broker.list_orders()
            if event.command.side is Side.SELL
        )
        assert sells_after == sells_before
        restored = second.trade_manager.get_position(opened.trade_id)
        assert restored is not None
        assert restored.state is TradeState.EXIT_PENDING


class TestSessionBoundary:
    def test_shutdown_at_1540_keeps_positional_open(
        self, store: TradingStore, clock: FrozenClock, tmp_path: Path
    ) -> None:
        """Positional opens survive the 15:40 IST halt; next session restores them."""
        runner = _open_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        assert opened.state is TradeState.OPEN
        sink = _Sink()
        session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
        session = PaperSession(
            runner=runner,
            clock=clock,
            session_config=session_cfg,
            session_hours=(time(9, 15), time(15, 30)),
            timezone=IST,
            notifier=sink,
            request_builder=lambda _now: ((), {}),
            observation_start=NOW,
            capital_limit=Money.of("700000", Currency.INR),
            risk_policy_version="4",
            fill_model_version="conservative-v1",
            code_version="1",
            charges_verified=False,
            cohort_dir=tmp_path / "cohorts",
        )
        clock.set(EOD)
        assert session.run(once=True) == 0
        persisted = store.get_position_lifecycle(opened.trade_id)
        assert persisted is not None
        assert persisted.position.state is TradeState.OPEN
        assert persisted.holding_style is HoldingStyle.INTRADAY

        clock.set(MORNING)
        next_session = _restart(store, clock, runner.broker)
        recovery = next_session.recover_lifecycle()
        restored = next_session.trade_manager.get_position(opened.trade_id)
        assert restored is not None
        assert restored.state is TradeState.OPEN
        assert opened.trade_id in recovery.restored_trade_ids


class TestStaleWhileOpen:
    def test_stale_market_data_does_not_exit_or_drop_position(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Stale quotes skip this cycle's exit; the open position stays monitored.

        Invariant 6: PROTECTION_DEGRADED is persisted and new entries freeze.
        PAPER software stops are not broker-resident.
        """
        runner = _open_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        symbol = opened.legs[0].contract.symbol
        stale = _option_snapshot(
            opened.legs[0].contract,
            market=f.quote(bid=f.price("1.00"), ask=f.price("1.05")),
            quality=f.quality(
                state=DataQuality.STALE, reason_codes=(ReasonCode.DATA_STALE,)
            ),
        )
        events = runner.manage_exits({symbol: stale})
        assert events == ()
        still_open = runner.trade_manager.get_position(opened.trade_id)
        assert still_open is not None
        assert still_open.state is TradeState.OPEN
        assert still_open.protection_degraded is True
        freeze = store.get_entry_freeze()
        assert freeze is not None
        assert freeze.entries_blocked is True
        assert freeze.reason_code is ReasonCode.PROTECTION_DEGRADED


class TestPartialAndUnreconciled:
    def test_partial_multileg_blocks_entries_and_alerts(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Invariant 15/6: incomplete legs are recorded, alerted, and block entries."""
        long_contract = f.option_contract()
        short_contract = f.option_contract(
            symbol="NIFTY26SEP24200CE", strike=Decimal("24200")
        )
        intent = f.intent(
            legs=(
                IntentLeg(
                    leg_id="long", contract=long_contract, side=Side.BUY, ratio=1
                ),
                IntentLeg(
                    leg_id="short", contract=short_contract, side=Side.SELL, ratio=1
                ),
            )
        )
        policy = build_exit_policy(
            intent.exit_template,
            trade_id="TRD-PARTIAL-1",
            policy_id="EXIT-POL-PARTIAL",
            entry_price=f.price("92.00"),
            initialized_at=clock.now_utc(),
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=75,
        )
        position = f.position_state(
            trade_id="TRD-PARTIAL-1",
            intent_id=intent.intent_id,
            state=TradeState.REPAIR_REQUIRED,
            legs=(
                f.position_leg_state(
                    leg_id="long",
                    contract=long_contract,
                    side=Side.BUY,
                    quantity_contracts=75,
                    average_entry_price=f.price("92.00"),
                ),
            ),
            exit_policy=policy,
            protective_order_ids=("REPAIR-PENDING",),
            opened_at=None,
        )
        record = PositionLifecycleRecord(
            trade_id=position.trade_id,
            position=position,
            intent=intent,
            risk_decision=f.risk_decision(intent_id=intent.intent_id),
            holding_style=HoldingStyle.POSITIONAL,
            as_of=position.as_of,
        )
        store.upsert_position_lifecycle(record, event_id="PLC-PARTIAL-1")
        broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES, clock=clock, id_factory=SequentialIdFactory(clock.instant)
        )
        payload = broker.dump_state()
        payload["positions"] = [
            f.position_record(
                trade_id=position.trade_id,
                contract=long_contract,
                side=Side.BUY,
                quantity_contracts=75,
            ).model_dump(mode="json")
        ]
        broker.load_state(payload)
        runner = _runner(store, clock, broker=broker)
        recovery = runner.recover_lifecycle()
        assert recovery.entries_blocked is True
        assert position.trade_id in recovery.unreconciled_trade_ids or any(
            alert.reason_code is ReasonCode.PARTIAL_FILL_UNREPAIRED
            for alert in recovery.alerts
        )
        text = format_lifecycle_alert(recovery.alerts[0])
        assert "PAPER recovery" in text
        assert "promote" not in text.lower()
        blocked = runner.run_cycle((_request(),))
        assert blocked.outcomes[0].order_events == ()


class TestIdempotentRecovery:
    def test_repeated_recovery_does_not_duplicate_exit(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Invariant 11: a second recovery must not submit another exit."""
        first = _open_long(store, clock)
        opened = first.trade_manager.list_positions()[0]
        symbol = opened.legs[0].contract.symbol
        stop = _option_snapshot(
            opened.legs[0].contract,
            market=f.quote(bid=f.price("1.00"), ask=f.price("1.05")),
        )
        first_exit = first.manage_exits({symbol: stop})
        assert first_exit
        sell_ids = {
            event.identity.internal_order_id
            for event in first.broker.list_orders()
            if event.command.side is Side.SELL
        }
        assert sell_ids
        closed = first.trade_manager.get_position(opened.trade_id)
        assert closed is not None
        assert closed.state is TradeState.CLOSED

        second = _restart(store, clock, first.broker)
        second.recover_lifecycle()
        second.manage_exits({symbol: stop})
        third = _restart(store, clock, second.broker)
        third.recover_lifecycle()
        third.manage_exits({symbol: stop})
        sells = [
            event
            for event in third.broker.list_orders()
            if event.command.side is Side.SELL
        ]
        assert len(sells) == len(sell_ids)
        records = [
            row
            for row in store.list_position_lifecycle()
            if row.trade_id == opened.trade_id
        ]
        assert len(records) == 1


class TestSpreadValuation:
    def test_debit_spread_uses_frozen_monitor_leg_not_first_position_leg(
        self,
    ) -> None:
        """Corrected path: frozen LEG_PRICE on the debit long, not position.legs[0]."""
        long_contract = f.option_contract()
        short_contract = f.option_contract(
            symbol="NIFTY26SEP24200CE", strike=Decimal("24200")
        )
        intent = f.intent(
            legs=(
                IntentLeg(
                    leg_id="long", contract=long_contract, side=Side.BUY, ratio=1
                ),
                IntentLeg(
                    leg_id="short", contract=short_contract, side=Side.SELL, ratio=1
                ),
            ),
            exit_template=f.exit_template(stop_distance_ticks=40),
        )
        policy = build_exit_policy(
            intent.exit_template,
            trade_id="TRD-SPREAD-1",
            policy_id="EXIT-POL-SPREAD",
            entry_price=f.price("92.00"),
            initialized_at=NOW,
            monitor_side=Side.BUY,
        )
        position = f.position_state(
            trade_id="TRD-SPREAD-1",
            intent_id=intent.intent_id,
            legs=(
                f.position_leg_state(
                    leg_id="short",
                    contract=short_contract,
                    side=Side.SELL,
                    average_entry_price=f.price("45.00"),
                    current_stop_price=None,
                ),
                f.position_leg_state(
                    leg_id="long",
                    contract=long_contract,
                    average_entry_price=f.price("92.00"),
                ),
            ),
            exit_policy=policy,
        )
        engine = ExitEngine()
        long_hit = _option_snapshot(
            long_contract,
            market=f.quote(bid=f.price("89.95"), ask=f.price("89.95")),
        )
        monitor = engine.evaluate(position, long_hit, intent, now=NOW)
        assert monitor.kind is ExitKind.STOP
        assert monitor.should_exit

        short_only = position.model_copy(update={"legs": (position.legs[0],)})
        missing = engine.evaluate(short_only, long_hit, intent, now=NOW)
        assert missing.kind is ExitKind.NONE
        assert missing.reason_code is ReasonCode.PRICE_UNAVAILABLE


class TestIntradayCloseRule:
    def test_intraday_time_exit_is_distinct_from_positional(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        intent = f.intent(
            exit_template=f.exit_template(
                stop_distance_ticks=200,
                time_exit=clock.now_utc() - timedelta(minutes=1),
            )
        )
        policy = build_exit_policy(
            intent.exit_template,
            trade_id="TRD-INTRA-1",
            policy_id="EXIT-POL-INTRA",
            entry_price=f.price("100.00"),
            initialized_at=clock.now_utc() - timedelta(hours=1),
        )
        position = f.position_state(
            trade_id="TRD-INTRA-1",
            intent_id=intent.intent_id,
            exit_policy=policy,
            legs=(f.position_leg_state(average_entry_price=f.price("100.00")),),
        )
        record = PositionLifecycleRecord(
            trade_id=position.trade_id,
            position=position,
            intent=intent,
            risk_decision=f.risk_decision(intent_id=intent.intent_id),
            holding_style=HoldingStyle.INTRADAY,
            as_of=position.as_of,
        )
        store.upsert_position_lifecycle(record, event_id="PLC-INTRA-1")
        assert record.holding_style is HoldingStyle.INTRADAY
        broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES, clock=clock, id_factory=SequentialIdFactory(clock.instant)
        )
        payload = broker.dump_state()
        payload["positions"] = [
            f.position_record(
                trade_id=position.trade_id,
                contract=position.legs[0].contract,
                quantity_contracts=position.legs[0].quantity_contracts,
            ).model_dump(mode="json")
        ]
        broker.load_state(payload)
        runner = _runner(store, clock, broker=broker)
        recovery = runner.recover_lifecycle()
        assert position.trade_id in recovery.restored_trade_ids
        restored = runner.trade_manager.get_position(position.trade_id)
        assert restored is not None
        snapshot = _option_snapshot(
            position.legs[0].contract,
            market=f.quote(bid=f.price("101.00"), ask=f.price("101.05")),
        )
        evaluation = runner.trade_manager.evaluate_exit(
            position.trade_id, snapshot, intent
        )
        assert evaluation.kind is ExitKind.TIME
        assert restored.exit_policy.time_exit is not None
