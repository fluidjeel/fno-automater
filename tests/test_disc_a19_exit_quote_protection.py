"""DISC-A19: exit quote cache, DISCOVERY protection staleness, partial-leg exits."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from tests.test_disc_a4_touch_fills import _discovery_broker
from tests.test_paper_lifecycle import _open_long, _option_snapshot, _restart
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config, _request
from trading.broker.paper import PaperBroker
from trading.config import load_evaluation_config, load_risk_policy
from trading.config.discovery import load_discovery_config
from trading.config.evaluation import discovery_fill_models
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    FeatureSnapshot,
    IntentLeg,
    PositionLegState,
    PositionLifecycleRecord,
    ReconciliationEvent,
)
from trading.domain.contracts.order import OrderEvent
from trading.domain.contracts.snapshot import SnapshotTimes
from trading.domain.enums import (
    DataQuality,
    DifferenceClass,
    ExitScope,
    HoldingStyle,
    OptionType,
    OrderState,
    ReasonCode,
    ReconciliationTrigger,
    Severity,
    Side,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Currency, Money
from trading.ops.paper_exit_recovery import retry_stuck_paper_exits
from trading.runtime.paper_runner import PaperRunner
from trading.storage.trading_store import TradingEventType, TradingStore
from trading.trade import ExitKind, build_exit_policy
from trading.trade.exits import ExitEvaluation

CYCLE_NOW = f.NOW + timedelta(seconds=60)
BAR_OPEN = CYCLE_NOW - timedelta(minutes=4)
DISCOVERY = load_discovery_config(ROOT / "config" / "discovery.yaml").config


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(CYCLE_NOW)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _discovery_runner(
    store: TradingStore, clock: FrozenClock, *, broker: PaperBroker | None = None
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
        discovery_config=DISCOVERY,
    )


def _bar_open_times() -> SnapshotTimes:
    return f.snapshot_times(
        event_time=BAR_OPEN,
        source_time=BAR_OPEN,
        receive_time=CYCLE_NOW - timedelta(seconds=5),
        calculation_time=CYCLE_NOW - timedelta(seconds=2),
    )


def _fresh_option(contract: object, *, bid: str, ask: str) -> FeatureSnapshot:
    return _option_snapshot(
        contract,
        market=f.quote(bid=f.price(bid), ask=f.price(ask)),
        times=_bar_open_times(),
        quality=f.quality(state=DataQuality.VALID, reason_codes=(ReasonCode.OK,)),
    )


def _exit_trigger_snapshot(contract: object, *, bid: str, ask: str) -> FeatureSnapshot:
    """Fresh event_time snapshot that triggers a stop without protection staleness."""
    return _option_snapshot(
        contract,
        market=f.quote(bid=f.price(bid), ask=f.price(ask)),
        times=f.snapshot_times(
            event_time=CYCLE_NOW,
            source_time=CYCLE_NOW,
            receive_time=CYCLE_NOW,
            calculation_time=CYCLE_NOW,
        ),
        quality=f.quality(state=DataQuality.VALID, reason_codes=(ReasonCode.OK,)),
    )


class TestExitQuotePublishing:
    def test_exit_publishes_quotes_before_fill(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Invariant 8: exits fill at current quotes even when entry cache is stale."""
        runner = _open_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        symbol = opened.legs[0].contract.symbol
        runner.broker._quotes.clear()
        stop = _exit_trigger_snapshot(opened.legs[0].contract, bid="1.00", ask="1.05")
        events = runner.manage_exits({symbol: stop})
        assert events
        assert all(event.state is not OrderState.REJECTED for event in events)
        assert all(
            event.reason_code is not ReasonCode.PRICE_UNAVAILABLE for event in events
        )
        closed = runner.trade_manager.get_position(opened.trade_id)
        assert closed is not None
        assert closed.state is TradeState.CLOSED

    def test_restart_then_exit_seeds_quote_cache(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """After restart the paper broker quote cache is empty until manage_exits."""
        first = _open_long(store, clock)
        opened = first.trade_manager.list_positions()[0]
        symbol = opened.legs[0].contract.symbol
        clock.set(clock.now_utc() + timedelta(minutes=5))
        second = _restart(store, clock, first.broker)
        second.recover_lifecycle()
        assert not second.broker._quotes
        now = clock.now_utc()
        stop = _option_snapshot(
            opened.legs[0].contract,
            market=f.quote(bid=f.price("1.00"), ask=f.price("1.05")),
            times=f.snapshot_times(
                event_time=now,
                source_time=now,
                receive_time=now,
                calculation_time=now,
            ),
            quality=f.quality(state=DataQuality.VALID, reason_codes=(ReasonCode.OK,)),
        )
        events = second.manage_exits({symbol: stop})
        assert events
        assert all(event.state is OrderState.FILLED for event in events)
        closed = second.trade_manager.get_position(opened.trade_id)
        assert closed is not None
        assert closed.state is TradeState.CLOSED


class TestDiscoveryProtectionStaleness:
    def test_discovery_does_not_degrade_on_bar_open_age(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """DISCOVERY protection uses quote_freshness_age_at, not bar event_time."""
        runner = _discovery_runner(store, clock)
        result = runner.run_cycle((_request(),))
        assert result.outcomes[0].order_events
        opened = runner.trade_manager.list_positions()[0]
        symbol = opened.legs[0].contract.symbol
        fresh = _fresh_option(opened.legs[0].contract, bid="91.95", ask="92.00")
        runner.manage_exits({symbol: fresh})
        still = runner.trade_manager.get_position(opened.trade_id)
        assert still is not None
        assert still.protection_degraded is False
        freeze = store.get_entry_freeze()
        assert freeze is None or freeze.entries_blocked is False

    def test_strict_still_degrades_on_bar_open_age(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """STRICT protection keeps event_time staleness semantics unchanged."""
        runner = _open_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        symbol = opened.legs[0].contract.symbol
        stale = _fresh_option(opened.legs[0].contract, bid="91.95", ask="92.00")
        runner.manage_exits({symbol: stale})
        still = runner.trade_manager.get_position(opened.trade_id)
        assert still is not None
        assert still.protection_degraded is True


class TestPartialLegExits:
    def test_straddle_with_one_leg_closed_still_exits_remaining(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """P&L-scoped trades evaluate and exit remaining legs after partial close."""
        put_contract = f.option_contract(
            symbol="NIFTY26SEP24500PE",
            strike=Decimal("24500"),
            option_type=OptionType.PUT,
        )
        call_contract = f.option_contract(
            symbol="NIFTY26SEP24500CE", strike=Decimal("24500")
        )
        intent = f.intent(
            legs=(
                IntentLeg(leg_id="put", contract=put_contract, side=Side.BUY, ratio=1),
                IntentLeg(
                    leg_id="call", contract=call_contract, side=Side.BUY, ratio=1
                ),
            )
        )
        policy = build_exit_policy(
            f.exit_template(stop_distance_ticks=1),
            trade_id="TRD-STRADDLE-HALF",
            policy_id="EXIT-HALF",
            entry_price=f.price("90.00"),
            initialized_at=clock.now_utc(),
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=65,
            entry_strategy_pnl=Money.of("0.00", Currency.INR),
        )
        position = f.position_state(
            trade_id="TRD-STRADDLE-HALF",
            intent_id=intent.intent_id,
            state=TradeState.OPEN,
            legs=(
                PositionLegState(
                    leg_id="call",
                    contract=call_contract,
                    side=Side.BUY,
                    quantity_contracts=65,
                    average_entry_price=f.price("88.00"),
                ),
            ),
            exit_policy=policy,
            protective_order_ids=("PROT-1",),
            opened_at=clock.now_utc(),
        )
        record = PositionLifecycleRecord(
            trade_id=position.trade_id,
            position=position,
            intent=intent,
            risk_decision=f.risk_decision(
                intent_id=intent.intent_id, capital_reservation_id="RES-HALF"
            ),
            holding_style=HoldingStyle.INTRADAY,
            as_of=position.as_of,
        )
        store.upsert_position_lifecycle(record, event_id="PLC-HALF")
        store.upsert_reservation(f.capital_reservation(reservation_id="RES-HALF"))
        broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES, clock=clock, id_factory=SequentialIdFactory(clock.instant)
        )
        payload = broker.dump_state()
        payload["positions"] = [
            f.position_record(
                trade_id=position.trade_id,
                contract=call_contract,
                side=Side.BUY,
                quantity_contracts=65,
            ).model_dump(mode="json")
        ]
        broker.load_state(payload)
        runner = _discovery_runner(store, clock, broker=broker)
        runner.recover_lifecycle()
        call_snap = _fresh_option(call_contract, bid="1.00", ask="1.05")
        events = runner.manage_exits({call_contract.symbol: call_snap})
        assert events
        assert all(event.state is OrderState.FILLED for event in events)
        closed = runner.trade_manager.get_position(position.trade_id)
        assert closed is not None
        assert closed.state is TradeState.CLOSED


def _paper_repo_root(tmp_path: Path) -> Path:
    import shutil

    session_root = tmp_path / "repo"
    (session_root / "config").mkdir(parents=True)
    (session_root / "data" / "paper").mkdir(parents=True)
    for name in (
        "paper_session.yaml",
        "paper.yaml",
        "discovery.yaml",
        "evaluation.yaml",
        "risk.yaml",
        "paper_data.yaml",
    ):
        shutil.copy(ROOT / "config" / name, session_root / "config" / name)
    return session_root


def _discovery_touch_runner(store: TradingStore, clock: FrozenClock) -> PaperRunner:
    ids = SequentialIdFactory(clock.instant)
    broker = _discovery_broker(clock)
    touch, shadow = discovery_fill_models(
        load_evaluation_config(ROOT / "config" / "evaluation.yaml").config.fill_model,
        model=DISCOVERY.fills.model,
        shadow_model=DISCOVERY.fills.shadow_model,
    )
    return PaperRunner(
        account_config=_paper_config(),  # type: ignore[arg-type]
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
        store=store,
        broker=broker,
        clock=clock,
        id_factory=ids,
        fill_model=touch,
        shadow_fill_model=shadow,
        discovery_config=DISCOVERY,
    )


def _open_discovery_long(store: TradingStore, clock: FrozenClock) -> PaperRunner:
    runner = _discovery_touch_runner(store, clock)
    result = runner.run_cycle((_request(),))
    assert result.outcomes[0].order_events
    assert runner.trade_manager.list_positions()
    return runner


def _reject_exit_without_quotes(
    runner: PaperRunner,
    trade_id: str,
    snapshots: dict[str, FeatureSnapshot],
) -> None:
    """Force PRICE_UNAVAILABLE on exit submit while leaving exit_order_ids set."""
    pending = runner.trade_manager.get_position(trade_id)
    assert pending is not None and pending.state is TradeState.EXIT_PENDING
    intent, decision = runner.open_book[trade_id]
    runner.broker._quotes.clear()
    publish = runner._publish_exit_quotes
    runner._publish_exit_quotes = lambda *args, **kwargs: None
    try:
        events = runner._submit_exit(intent, decision, pending, snapshots)
    finally:
        runner._publish_exit_quotes = publish
    assert events
    assert all(event.state is OrderState.REJECTED for event in events)
    assert all(event.reason_code is ReasonCode.PRICE_UNAVAILABLE for event in events)
    reopened = runner.trade_manager.get_position(trade_id)
    assert reopened is not None and reopened.state is TradeState.OPEN
    record = runner._services.store.get_position_lifecycle(trade_id)
    assert record is not None
    assert record.exit_order_ids == ()


def _persist_repo_for_retry(
    runner: PaperRunner,
    store: TradingStore,
    tmp_path: Path,
    *,
    quotes: dict[str, dict[str, str]],
) -> tuple[Path, Path]:
    import json
    import shutil

    session_root = _paper_repo_root(tmp_path)
    store_path = session_root / "data" / "paper" / "trading.sqlite"
    sqlite_src = tmp_path / "paper.sqlite"
    store.close()
    shutil.copy(sqlite_src, store_path)
    broker_state = session_root / "data" / "paper" / "broker_state.json"
    broker_state.write_text(
        json.dumps(runner.broker.dump_state(), indent=2) + "\n",
        encoding="utf-8",
    )
    quotes_path = session_root / "recovery_quotes.json"
    quotes_path.write_text(json.dumps(quotes), encoding="utf-8")
    return session_root, quotes_path


def _filled_exit_events(store: TradingStore, trade_id: str) -> tuple[OrderEvent, ...]:
    filled: list[OrderEvent] = []
    for stored in store.read_events():
        if stored.event_type is not TradingEventType.ORDER_EVENT:
            continue
        event = stored.deserialize()
        if not isinstance(event, OrderEvent):
            continue
        if (
            event.identity.trade_id == trade_id
            and event.state is OrderState.FILLED
            and event.command.side is Side.SELL
        ):
            filled.append(event)
    return tuple(filled)


class TestOpsRetryStuckPaperExits:
    def test_ops_retries_exit_pending_with_rejected_orders(
        self, store: TradingStore, clock: FrozenClock, tmp_path: Path
    ) -> None:
        """EXIT_PENDING with rejected exit leg closes at operator quotes."""
        runner = _open_discovery_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        symbol = opened.legs[0].contract.symbol
        runner.trade_manager.apply_exit_evaluation(
            opened.trade_id,
            ExitEvaluation(
                kind=ExitKind.STOP,
                reason_code=ReasonCode.OK,
                detail="seed exit pending",
                updated_policy=opened.exit_policy,
            ),
        )
        runner._write_lifecycle(opened.trade_id)
        snap = _option_snapshot(
            opened.legs[0].contract,
            market=f.quote(bid=f.price("1.00"), ask=f.price("1.05")),
        )
        _reject_exit_without_quotes(runner, opened.trade_id, {symbol: snap})
        rejected_id = next(
            stored.deserialize().identity.internal_order_id
            for stored in store.read_events()
            if stored.event_type is TradingEventType.ORDER_EVENT
            and stored.deserialize().state is OrderState.REJECTED
        )
        stuck_record = store.get_position_lifecycle(opened.trade_id)
        assert stuck_record is not None
        pending_position = stuck_record.position.model_copy(
            update={"state": TradeState.EXIT_PENDING}
        )
        store.upsert_position_lifecycle(
            stuck_record.model_copy(
                update={
                    "exit_order_ids": (rejected_id,),
                    "position": pending_position,
                }
            ),
            event_id="PLC-STUCK-EXIT",
        )
        runner.trade_manager.restore_position(pending_position)

        event = ReconciliationEvent(
            event_id="REC-STUCK",
            scope=f"trade/{opened.trade_id}/lifecycle",
            trigger=ReconciliationTrigger.BOOT,
            expected_local_ref=opened.trade_id,
            difference_class=DifferenceClass.UNEXPECTED_BROKER_STATE,
            severity=Severity.CRITICAL,
            reason_code=ReasonCode.UNKNOWN_ORDER_STATUS,
            repair_action="operator review",
            entries_blocked=True,
            detected_at=clock.now_utc(),
        )
        store.append(
            TradingEventType.RECONCILIATION_EVENT, event, event_id=event.event_id
        )

        session_root, quotes_path = _persist_repo_for_retry(
            runner,
            store,
            tmp_path,
            quotes={symbol: {"bid": "1.00", "ask": "1.05", "last": "1.00"}},
        )
        clock.set(clock.now_utc() + timedelta(minutes=5))

        result = retry_stuck_paper_exits(
            session_root,
            quotes_json=quotes_path,
            trade_id=opened.trade_id,
            clock=clock,
        )
        assert opened.trade_id in result.trade_ids
        assert result.resolved_event_ids

        reopened = TradingStore.open(
            session_root / "data" / "paper" / "trading.sqlite", clock=clock
        )
        closed = reopened.get_position_lifecycle(opened.trade_id)
        assert closed is not None
        assert closed.position.state is TradeState.CLOSED
        fills = _filled_exit_events(reopened, opened.trade_id)
        assert len(fills) == 1
        assert fills[0].average_fill_price == f.price("1.00")
        reopened.close()

    def test_ops_retries_open_trade_after_rejected_exit(
        self, store: TradingStore, clock: FrozenClock, tmp_path: Path
    ) -> None:
        """OPEN trade with a prior rejected exit closes at operator quotes."""
        runner = _open_discovery_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        symbol = opened.legs[0].contract.symbol
        runner.trade_manager.apply_exit_evaluation(
            opened.trade_id,
            ExitEvaluation(
                kind=ExitKind.STOP,
                reason_code=ReasonCode.OK,
                detail="seed exit pending",
                updated_policy=opened.exit_policy,
            ),
        )
        runner._write_lifecycle(opened.trade_id)
        snap = _option_snapshot(
            opened.legs[0].contract,
            market=f.quote(bid=f.price("2.00"), ask=f.price("2.05")),
        )
        _reject_exit_without_quotes(runner, opened.trade_id, {symbol: snap})
        rejected_id = next(
            stored.deserialize().identity.internal_order_id
            for stored in store.read_events()
            if stored.event_type is TradingEventType.ORDER_EVENT
            and stored.deserialize().state is OrderState.REJECTED
        )
        stuck_record = store.get_position_lifecycle(opened.trade_id)
        assert stuck_record is not None
        store.upsert_position_lifecycle(
            stuck_record.model_copy(update={"exit_order_ids": (rejected_id,)}),
            event_id="PLC-OPEN-REJECT",
        )
        assert (
            runner.trade_manager.get_position(opened.trade_id).state is TradeState.OPEN
        )

        session_root, quotes_path = _persist_repo_for_retry(
            runner,
            store,
            tmp_path,
            quotes={symbol: {"bid": "2.00", "ask": "2.05", "last": "2.00"}},
        )
        clock.set(clock.now_utc() + timedelta(minutes=5))

        result = retry_stuck_paper_exits(
            session_root,
            quotes_json=quotes_path,
            trade_id=opened.trade_id,
            clock=clock,
        )
        assert opened.trade_id in result.trade_ids

        reopened = TradingStore.open(
            session_root / "data" / "paper" / "trading.sqlite", clock=clock
        )
        closed = reopened.get_position_lifecycle(opened.trade_id)
        assert closed is not None
        assert closed.position.state is TradeState.CLOSED
        fills = _filled_exit_events(reopened, opened.trade_id)
        assert len(fills) == 1
        assert fills[0].average_fill_price == f.price("2.00")
        reopened.close()

    def test_ops_retries_strangle_exit_pending_first_leg_rejected(
        self, store: TradingStore, clock: FrozenClock, tmp_path: Path
    ) -> None:
        """Strangle EXIT_PENDING with one rejected leg closes both at quotes."""
        put_contract = f.option_contract(
            symbol="NIFTY26SEP24500PE",
            strike=Decimal("24500"),
            option_type=OptionType.PUT,
        )
        call_contract = f.option_contract(
            symbol="NIFTY26SEP24500CE", strike=Decimal("24500")
        )
        intent = f.intent(
            legs=(
                IntentLeg(leg_id="put", contract=put_contract, side=Side.BUY, ratio=1),
                IntentLeg(
                    leg_id="call", contract=call_contract, side=Side.BUY, ratio=1
                ),
            )
        )
        policy = build_exit_policy(
            f.exit_template(stop_distance_ticks=1),
            trade_id="TRD-STRANGLE-STUCK",
            policy_id="EXIT-STRANGLE",
            entry_price=f.price("90.00"),
            initialized_at=clock.now_utc(),
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=65,
            entry_strategy_pnl=Money.of("0.00", Currency.INR),
        )
        position = f.position_state(
            trade_id="TRD-STRANGLE-STUCK",
            intent_id=intent.intent_id,
            state=TradeState.OPEN,
            legs=(
                PositionLegState(
                    leg_id="put",
                    contract=put_contract,
                    side=Side.BUY,
                    quantity_contracts=65,
                    average_entry_price=f.price("88.00"),
                ),
                PositionLegState(
                    leg_id="call",
                    contract=call_contract,
                    side=Side.BUY,
                    quantity_contracts=65,
                    average_entry_price=f.price("92.00"),
                ),
            ),
            exit_policy=policy,
            protective_order_ids=("PROT-1",),
            opened_at=clock.now_utc(),
        )
        record = PositionLifecycleRecord(
            trade_id=position.trade_id,
            position=position,
            intent=intent,
            risk_decision=f.risk_decision(
                intent_id=intent.intent_id, capital_reservation_id="RES-STRANGLE"
            ),
            holding_style=HoldingStyle.INTRADAY,
            as_of=position.as_of,
        )
        store.upsert_position_lifecycle(record, event_id="PLC-STRANGLE")
        store.upsert_reservation(f.capital_reservation(reservation_id="RES-STRANGLE"))
        broker = _discovery_broker(clock)
        payload = broker.dump_state()
        payload["positions"] = [
            f.position_record(
                trade_id=position.trade_id,
                contract=put_contract,
                side=Side.BUY,
                quantity_contracts=65,
            ).model_dump(mode="json"),
            f.position_record(
                trade_id=position.trade_id,
                contract=call_contract,
                side=Side.BUY,
                quantity_contracts=65,
            ).model_dump(mode="json"),
        ]
        broker.load_state(payload)
        runner = _discovery_touch_runner(store, clock)
        runner.broker.load_state(payload)
        runner.recover_lifecycle()
        runner.trade_manager.apply_exit_evaluation(
            position.trade_id,
            ExitEvaluation(
                kind=ExitKind.STOP,
                reason_code=ReasonCode.OK,
                detail="seed exit pending",
                updated_policy=policy,
            ),
        )
        runner._write_lifecycle(position.trade_id)
        put_snap = _option_snapshot(
            put_contract, market=f.quote(bid=f.price("3.00"), ask=f.price("3.05"))
        )
        call_snap = _option_snapshot(
            call_contract, market=f.quote(bid=f.price("4.00"), ask=f.price("4.05"))
        )
        _reject_exit_without_quotes(
            runner,
            position.trade_id,
            {put_contract.symbol: put_snap, call_contract.symbol: call_snap},
        )
        rejected_id = next(
            stored.deserialize().identity.internal_order_id
            for stored in store.read_events()
            if stored.event_type is TradingEventType.ORDER_EVENT
            and stored.deserialize().state is OrderState.REJECTED
        )
        stuck_record = store.get_position_lifecycle(position.trade_id)
        assert stuck_record is not None
        pending_position = stuck_record.position.model_copy(
            update={"state": TradeState.EXIT_PENDING}
        )
        store.upsert_position_lifecycle(
            stuck_record.model_copy(
                update={
                    "exit_order_ids": (rejected_id,),
                    "position": pending_position,
                }
            ),
            event_id="PLC-STRANGLE-STUCK",
        )
        runner.trade_manager.restore_position(pending_position)

        session_root, quotes_path = _persist_repo_for_retry(
            runner,
            store,
            tmp_path,
            quotes={
                put_contract.symbol: {"bid": "3.00", "ask": "3.05", "last": "3.00"},
                call_contract.symbol: {"bid": "4.00", "ask": "4.05", "last": "4.00"},
            },
        )
        clock.set(clock.now_utc() + timedelta(minutes=5))

        result = retry_stuck_paper_exits(
            session_root,
            quotes_json=quotes_path,
            trade_id=position.trade_id,
            clock=clock,
        )
        assert position.trade_id in result.trade_ids

        reopened = TradingStore.open(
            session_root / "data" / "paper" / "trading.sqlite", clock=clock
        )
        closed = reopened.get_position_lifecycle(position.trade_id)
        assert closed is not None
        assert closed.position.state is TradeState.CLOSED
        fills = _filled_exit_events(reopened, position.trade_id)
        assert len(fills) == 2
        fill_prices = {event.average_fill_price for event in fills}
        assert fill_prices == {f.price("3.00"), f.price("4.00")}
        reopened.close()
