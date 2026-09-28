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
from trading.broker.paper import PaperBroker
from trading.domain.clock import FrozenClock
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


class TestOraclePersistedShape:
    """Reproduce Oracle trades …017 (partial straddle) and …045 (strangle)."""

    def test_partial_straddle_open_empty_exit_order_ids_sqlite_idem(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        """017: one CE leg left, PE filled, CE rejected, exit_order_ids cleared."""
        from tests.test_disc_a4_touch_fills import _discovery_broker
        from trading.domain.ids import derive_idempotency_key

        call_contract = f.option_contract(
            symbol="NSE:NIFTY26O0622950CE",
            strike=Decimal("22950"),
        )
        put_contract = f.option_contract(
            symbol="NSE:NIFTY26O0622950PE",
            strike=Decimal("22950"),
            option_type=OptionType.PUT,
        )
        intent = f.intent(
            legs=(
                IntentLeg(
                    leg_id="leg-put", contract=put_contract, side=Side.BUY, ratio=1
                ),
                IntentLeg(
                    leg_id="leg-call", contract=call_contract, side=Side.BUY, ratio=1
                ),
            )
        )
        policy = build_exit_policy(
            f.exit_template(stop_distance_ticks=200),
            trade_id="TRD-ORACLE-017",
            policy_id="EXIT-017",
            entry_price=f.price("176.05"),
            initialized_at=clock.now_utc(),
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=65,
            entry_strategy_pnl=Money.of("0.00", Currency.INR),
        )
        position = f.position_state(
            trade_id="TRD-ORACLE-017",
            intent_id=intent.intent_id,
            state=TradeState.OPEN,
            legs=(
                PositionLegState(
                    leg_id="leg-call",
                    contract=call_contract,
                    side=Side.BUY,
                    quantity_contracts=65,
                    average_entry_price=f.price("176.05"),
                ),
            ),
            entry_legs=(
                PositionLegState(
                    leg_id="leg-put",
                    contract=put_contract,
                    side=Side.BUY,
                    quantity_contracts=65,
                    average_entry_price=f.price("200.65"),
                ),
                PositionLegState(
                    leg_id="leg-call",
                    contract=call_contract,
                    side=Side.BUY,
                    quantity_contracts=65,
                    average_entry_price=f.price("176.05"),
                ),
            ),
            exit_policy=policy,
            protective_order_ids=("PROT-019", "PROT-021"),
            opened_at=clock.now_utc(),
        )
        record = PositionLifecycleRecord(
            trade_id=position.trade_id,
            position=position,
            intent=intent,
            risk_decision=f.risk_decision(
                intent_id=intent.intent_id, capital_reservation_id="RES-017"
            ),
            holding_style=HoldingStyle.INTRADAY,
            as_of=position.as_of,
            exit_order_ids=(),
        )
        store.upsert_position_lifecycle(record, event_id="PLC-017")
        store.upsert_reservation(f.capital_reservation(reservation_id="RES-017"))
        ce_idem = derive_idempotency_key(
            account_id="ACC-PAPER-1",
            strategy_id=intent.strategy_id,
            strategy_version=intent.strategy_version,
            intent_id=intent.intent_id,
            leg_id="leg-call-exit",
            side=Side.SELL.value,
            quantity_contracts=65,
        )
        rejected_ce = f.order_event(
            event_id="EVT-017-CE-REJECT",
            identity=f.order_identity(
                internal_order_id="ORD-017-099",
                trade_id=position.trade_id,
                idempotency_key=ce_idem,
            ),
            command=f.order_command(
                contract=call_contract,
                side=Side.SELL,
                quantity_contracts=65,
                limit_price=f.price("168.20"),
            ),
            state=OrderState.REJECTED,
            reason_code=ReasonCode.PRICE_UNAVAILABLE,
            acknowledged_quantity=0,
            received_at=clock.now_utc(),
        )
        store.append(
            TradingEventType.ORDER_EVENT, rejected_ce, event_id=rejected_ce.event_id
        )
        store.register_idempotency_key(ce_idem, rejected_ce.event_id)
        filled_pe = f.order_event(
            event_id="EVT-017-PE-FILL",
            identity=f.order_identity(
                internal_order_id="ORD-017-PE",
                trade_id=position.trade_id,
            ),
            command=f.order_command(
                contract=put_contract,
                side=Side.SELL,
                quantity_contracts=65,
                limit_price=f.price("203.20"),
            ),
            state=OrderState.FILLED,
            average_fill_price=f.price("203.20"),
            filled_quantity=65,
            acknowledged_quantity=65,
            received_at=clock.now_utc(),
        )
        broker = _discovery_broker(clock)
        payload = broker.dump_state()
        payload["positions"] = [
            f.position_record(
                trade_id=position.trade_id,
                contract=call_contract,
                side=Side.BUY,
                quantity_contracts=65,
            ).model_dump(mode="json")
        ]
        payload["orders"] = [
            filled_pe.model_dump(mode="json"),
            rejected_ce.model_dump(mode="json"),
        ]
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
                    "NSE:NIFTY26O0622950CE": {
                        "bid": "170.00",
                        "ask": "170.05",
                        "last": "170.00",
                    }
                }
            ),
            encoding="utf-8",
        )

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
        ce_fills = [
            event
            for event in fills
            if event.command.contract.symbol == call_contract.symbol
        ]
        assert len(ce_fills) == 1
        assert ce_fills[0].average_fill_price == f.price("170.00")
        idem_row = reopened._conn.execute(
            "SELECT 1 FROM idempotency_keys WHERE idempotency_key = ?",
            (ce_idem,),
        ).fetchone()
        assert idem_row is not None
        broker_payload = json.loads(broker_state.read_text(encoding="utf-8"))
        broker_orders = broker_payload.get("orders", [])
        assert any(
            isinstance(row, dict)
            and row.get("identity", {}).get("internal_order_id") == "ORD-017-PE"
            and row.get("state") == OrderState.FILLED.value
            for row in broker_orders
        )
        assert not any(
            isinstance(row, dict) and row.get("state") == OrderState.REJECTED.value
            for row in broker_orders
        )
        reopened.close()

    def test_strangle_open_empty_exit_order_ids_pe_rejected_only(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        """045: both legs open, PE rejected in broker, CE never submitted."""
        from tests.test_disc_a4_touch_fills import _discovery_broker
        from trading.domain.ids import derive_idempotency_key

        put_contract = f.option_contract(
            symbol="NSE:NIFTY26O0622800PE",
            strike=Decimal("22800"),
            option_type=OptionType.PUT,
        )
        call_contract = f.option_contract(
            symbol="NSE:NIFTY26O0622900CE",
            strike=Decimal("22900"),
        )
        intent = f.intent(
            legs=(
                IntentLeg(
                    leg_id="leg-put", contract=put_contract, side=Side.BUY, ratio=1
                ),
                IntentLeg(
                    leg_id="leg-call", contract=call_contract, side=Side.BUY, ratio=1
                ),
            )
        )
        policy = build_exit_policy(
            f.exit_template(stop_distance_ticks=200),
            trade_id="TRD-ORACLE-045",
            policy_id="EXIT-045",
            entry_price=f.price("170.00"),
            initialized_at=clock.now_utc(),
            scope=ExitScope.STRATEGY_PNL,
            quantity_contracts=65,
            entry_strategy_pnl=Money.of("0.00", Currency.INR),
        )
        position = f.position_state(
            trade_id="TRD-ORACLE-045",
            intent_id=intent.intent_id,
            state=TradeState.OPEN,
            legs=(
                PositionLegState(
                    leg_id="leg-put",
                    contract=put_contract,
                    side=Side.BUY,
                    quantity_contracts=65,
                    average_entry_price=f.price("137.25"),
                ),
                PositionLegState(
                    leg_id="leg-call",
                    contract=call_contract,
                    side=Side.BUY,
                    quantity_contracts=65,
                    average_entry_price=f.price("202.75"),
                ),
            ),
            exit_policy=policy,
            protective_order_ids=("PROT-045",),
            opened_at=clock.now_utc(),
        )
        record = PositionLifecycleRecord(
            trade_id=position.trade_id,
            position=position,
            intent=intent,
            risk_decision=f.risk_decision(
                intent_id=intent.intent_id, capital_reservation_id="RES-045"
            ),
            holding_style=HoldingStyle.INTRADAY,
            as_of=position.as_of,
            exit_order_ids=(),
        )
        store.upsert_position_lifecycle(record, event_id="PLC-045")
        store.upsert_reservation(f.capital_reservation(reservation_id="RES-045"))
        pe_idem = derive_idempotency_key(
            account_id="ACC-PAPER-1",
            strategy_id=intent.strategy_id,
            strategy_version=intent.strategy_version,
            intent_id=intent.intent_id,
            leg_id="leg-put-exit",
            side=Side.SELL.value,
            quantity_contracts=65,
        )
        rejected_pe = f.order_event(
            event_id="EVT-045-PE-REJECT",
            identity=f.order_identity(
                internal_order_id="ORD-045-116",
                trade_id=position.trade_id,
                idempotency_key=pe_idem,
            ),
            command=f.order_command(
                contract=put_contract,
                side=Side.SELL,
                quantity_contracts=65,
                limit_price=f.price("139.20"),
            ),
            state=OrderState.REJECTED,
            reason_code=ReasonCode.PRICE_UNAVAILABLE,
            acknowledged_quantity=0,
            received_at=clock.now_utc(),
        )
        store.append(
            TradingEventType.ORDER_EVENT, rejected_pe, event_id=rejected_pe.event_id
        )
        store.register_idempotency_key(pe_idem, rejected_pe.event_id)
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
        payload["orders"] = [rejected_pe.model_dump(mode="json")]
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
                    "NSE:NIFTY26O0622800PE": {
                        "bid": "140.00",
                        "ask": "140.05",
                        "last": "140.00",
                    },
                    "NSE:NIFTY26O0622900CE": {
                        "bid": "205.00",
                        "ask": "205.05",
                        "last": "205.00",
                    },
                }
            ),
            encoding="utf-8",
        )

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
        assert len(fills) == 2
        fill_prices = {event.average_fill_price for event in fills}
        assert fill_prices == {f.price("140.00"), f.price("205.00")}
        reopened.close()
