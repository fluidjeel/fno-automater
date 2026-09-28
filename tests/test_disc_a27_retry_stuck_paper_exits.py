"""DISC-A27: retry-stuck-paper-exits on realistic persisted PAPER state."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from tests.test_disc_a19_exit_quote_protection import (
    _discovery_touch_runner,
    _filled_exit_events,
    _open_discovery_long,
    _paper_repo_root,
    _persist_repo_for_retry,
    _reject_exit_without_quotes,
)
from tests.test_paper_lifecycle import _option_snapshot
from trading.domain.clock import FrozenClock
from trading.broker.paper import PaperBroker
from trading.domain.contracts import (
    IntentLeg,
    PositionLegState,
    PositionLifecycleRecord,
)
from trading.domain.contracts.order import OrderEvent
from trading.domain.enums import (
    ExitScope,
    HoldingStyle,
    OptionType,
    OrderState,
    ReasonCode,
    Side,
    TradeState,
)
from trading.domain.primitives import Currency, Money
from trading.ops.paper_exit_recovery import retry_stuck_paper_exits
from trading.storage.trading_store import TradingEventType, TradingStore
from trading.trade import ExitKind, build_exit_policy
from trading.trade.exits import ExitEvaluation

CYCLE_NOW = f.NOW + timedelta(seconds=60)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(CYCLE_NOW)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _strip_exit_order_events(trading_store: TradingStore) -> None:
    """Simulate broker_state retaining rejected exits that SQLite no longer indexes."""
    exit_keys: list[str] = []
    for stored in trading_store.read_events():
        if stored.event_type is not TradingEventType.ORDER_EVENT:
            continue
        event = stored.deserialize()
        if not isinstance(event, OrderEvent):
            continue
        if event.command.side is not Side.SELL:
            continue
        trading_store._conn.execute(
            "DELETE FROM trading_events WHERE event_id = ?",
            (stored.event_id,),
        )
        if stored.idempotency_key is not None:
            exit_keys.append(stored.idempotency_key)
    for key in exit_keys:
        trading_store._conn.execute(
            "DELETE FROM idempotency_keys WHERE idempotency_key = ?",
            (key,),
        )
    trading_store._conn.commit()


def _first_broker_rejected_order(broker: PaperBroker) -> OrderEvent:
    for event in broker.list_orders():
        if event.state is OrderState.REJECTED:
            return event
    msg = "expected a rejected broker order"
    raise AssertionError(msg)


class TestOpsRetryRealPersistedState:
    def test_broker_only_rejected_exit_pending_closes_at_quotes(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """EXIT_PENDING with rejected order only in broker_state, not SQLite."""
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
        _reject_exit_without_quotes(
            monkeypatch, runner, opened.trade_id, {symbol: snap}
        )
        rejected = _first_broker_rejected_order(runner.broker)
        _strip_exit_order_events(store)
        stuck_record = store.get_position_lifecycle(opened.trade_id)
        assert stuck_record is not None
        pending_position = stuck_record.position.model_copy(
            update={"state": TradeState.EXIT_PENDING}
        )
        store.upsert_position_lifecycle(
            stuck_record.model_copy(
                update={
                    "exit_order_ids": (rejected.identity.internal_order_id,),
                    "position": pending_position,
                }
            ),
            event_id="PLC-BROKER-ONLY",
        )
        runner.trade_manager.restore_position(pending_position)

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
            verbose=True,
        )
        assert opened.trade_id in result.trade_ids
        assert result.verbose_log

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

    def test_partial_straddle_remaining_leg_closes(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        """Single remaining CE leg after PE exit; frozen intent still has both legs."""
        call_contract = f.option_contract(
            symbol="NIFTY26SEP22950CE",
            strike=Decimal("22950"),
        )
        put_contract = f.option_contract(
            symbol="NIFTY26SEP22950PE",
            strike=Decimal("22950"),
            option_type=OptionType.PUT,
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
            trade_id="TRD-PARTIAL-STRADDLE",
            policy_id="EXIT-PARTIAL",
            entry_price=f.price("90.00"),
            initialized_at=clock.now_utc(),
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=65,
            entry_strategy_pnl=Money.of("0.00", Currency.INR),
        )
        position = f.position_state(
            trade_id="TRD-PARTIAL-STRADDLE",
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
                intent_id=intent.intent_id, capital_reservation_id="RES-PARTIAL"
            ),
            holding_style=HoldingStyle.INTRADAY,
            as_of=position.as_of,
            exit_order_ids=("ORD-PE-REJECT",),
        )
        store.upsert_position_lifecycle(record, event_id="PLC-PARTIAL")
        store.upsert_reservation(f.capital_reservation(reservation_id="RES-PARTIAL"))
        from tests.test_disc_a4_touch_fills import _discovery_broker

        broker = _discovery_broker(clock)
        rejected = f.order_event(
            event_id="EVT-PE-REJECT",
            identity=f.order_identity(
                internal_order_id="ORD-PE-REJECT",
                trade_id=position.trade_id,
            ),
            command=f.order_command(
                contract=put_contract,
                side=Side.SELL,
                quantity_contracts=65,
            ),
            state=OrderState.REJECTED,
            reason_code=ReasonCode.PRICE_UNAVAILABLE,
            acknowledged_quantity=0,
            received_at=clock.now_utc(),
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
        payload["orders"] = [rejected.model_dump(mode="json")]
        broker.load_state(payload)
        runner = _discovery_touch_runner(store, clock)
        runner.broker.load_state(payload)
        runner.recover_lifecycle()

        session_root = _paper_repo_root(tmp_path)
        store_path = session_root / "data" / "paper" / "trading.sqlite"
        store.close()
        import shutil

        shutil.copy(tmp_path / "paper.sqlite", store_path)
        broker_state = session_root / "data" / "paper" / "broker_state.json"
        broker_state.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        quotes_path = session_root / "recovery_quotes.json"
        quotes_path.write_text(
            json.dumps(
                {call_contract.symbol: {"bid": "2.50", "ask": "2.55", "last": "2.50"}}
            ),
            encoding="utf-8",
        )
        clock.set(clock.now_utc() + timedelta(minutes=5))

        result = retry_stuck_paper_exits(
            session_root,
            quotes_json=quotes_path,
            trade_id=position.trade_id,
            clock=clock,
        )
        assert position.trade_id in result.trade_ids

        reopened = TradingStore.open(store_path, clock=clock)
        closed = reopened.get_position_lifecycle(position.trade_id)
        assert closed is not None
        assert closed.position.state is TradeState.CLOSED
        fills = _filled_exit_events(reopened, position.trade_id)
        assert len(fills) == 1
        assert fills[0].average_fill_price == f.price("2.50")
        reopened.close()

    def test_strangle_exit_pending_first_leg_rejected_broker_only(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        """Strangle EXIT_PENDING: PE rejected in broker only; CE never submitted."""
        put_contract = f.option_contract(
            symbol="NIFTY26SEP22800PE",
            strike=Decimal("22800"),
            option_type=OptionType.PUT,
        )
        call_contract = f.option_contract(
            symbol="NIFTY26SEP22900CE", strike=Decimal("22900")
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
            trade_id="TRD-STRANGLE-BROKER",
            policy_id="EXIT-STRANGLE",
            entry_price=f.price("90.00"),
            initialized_at=clock.now_utc(),
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=65,
            entry_strategy_pnl=Money.of("0.00", Currency.INR),
        )
        position = f.position_state(
            trade_id="TRD-STRANGLE-BROKER",
            intent_id=intent.intent_id,
            state=TradeState.EXIT_PENDING,
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
            exit_order_ids=("ORD-PE-REJECT",),
        )
        store.upsert_position_lifecycle(record, event_id="PLC-STRANGLE-BROKER")
        store.upsert_reservation(f.capital_reservation(reservation_id="RES-STRANGLE"))
        from tests.test_disc_a4_touch_fills import _discovery_broker

        broker = _discovery_broker(clock)
        rejected = f.order_event(
            event_id="EVT-PE-REJECT",
            identity=f.order_identity(
                internal_order_id="ORD-PE-REJECT",
                trade_id=position.trade_id,
            ),
            command=f.order_command(
                contract=put_contract,
                side=Side.SELL,
                quantity_contracts=65,
            ),
            state=OrderState.REJECTED,
            reason_code=ReasonCode.PRICE_UNAVAILABLE,
            acknowledged_quantity=0,
            received_at=clock.now_utc(),
        )
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
        payload["orders"] = [rejected.model_dump(mode="json")]
        broker.load_state(payload)
        runner = _discovery_touch_runner(store, clock)
        runner.broker.load_state(payload)
        runner.recover_lifecycle()

        session_root = _paper_repo_root(tmp_path)
        store_path = session_root / "data" / "paper" / "trading.sqlite"
        store.close()
        import shutil

        shutil.copy(tmp_path / "paper.sqlite", store_path)
        broker_state = session_root / "data" / "paper" / "broker_state.json"
        broker_state.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        quotes_path = session_root / "recovery_quotes.json"
        quotes_path.write_text(
            json.dumps(
                {
                    put_contract.symbol: {
                        "bid": "3.00",
                        "ask": "3.05",
                        "last": "3.00",
                    },
                    call_contract.symbol: {
                        "bid": "4.00",
                        "ask": "4.05",
                        "last": "4.00",
                    },
                }
            ),
            encoding="utf-8",
        )
        clock.set(clock.now_utc() + timedelta(minutes=5))

        result = retry_stuck_paper_exits(
            session_root,
            quotes_json=quotes_path,
            trade_id=position.trade_id,
            clock=clock,
            verbose=True,
        )
        assert position.trade_id in result.trade_ids
        assert not result.leg_skips

        reopened = TradingStore.open(store_path, clock=clock)
        closed = reopened.get_position_lifecycle(position.trade_id)
        assert closed is not None
        assert closed.position.state is TradeState.CLOSED
        fills = _filled_exit_events(reopened, position.trade_id)
        assert len(fills) == 2
        fill_prices = {event.average_fill_price for event in fills}
        assert fill_prices == {f.price("3.00"), f.price("4.00")}
        reopened.close()

    def test_missing_quote_reports_per_leg_skip(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        """Operator quote book missing one leg prints a per-leg skip reason."""
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
        stuck_record = store.get_position_lifecycle(opened.trade_id)
        assert stuck_record is not None
        store.upsert_position_lifecycle(
            stuck_record.model_copy(
                update={
                    "position": stuck_record.position.model_copy(
                        update={"state": TradeState.EXIT_PENDING}
                    )
                }
            ),
            event_id="PLC-MISSING-QUOTE",
        )
        session_root, quotes_path = _persist_repo_for_retry(
            runner,
            store,
            tmp_path,
            quotes={},
        )
        quotes_path.write_text("{}", encoding="utf-8")

        result = retry_stuck_paper_exits(
            session_root,
            quotes_json=quotes_path,
            trade_id=opened.trade_id,
            clock=clock,
        )
        assert opened.trade_id not in result.trade_ids
        assert any(symbol in line for line in result.leg_skips)
