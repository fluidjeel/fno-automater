"""DISC-A30: PAPER broker_state durability, startup repair, stuck-exit selection."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

import tests.factories as f
from tests.test_disc_a19_exit_quote_protection import (
    _open_discovery_long,
    _persist_repo_for_retry,
)
from tests.test_paper_broker import _submit_request
from tests.test_paper_runner import BROKER_FIXTURES, ROOT, _paper_config
from trading.broker.paper import PaperBroker, repair_paper_broker_from_store
from trading.broker.paper.persistence import atomic_write_json
from trading.config import load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts.order import OrderEvent
from trading.domain.enums import OrderState, ReasonCode
from trading.domain.ids import SequentialIdFactory
from trading.ops.paper_exit_recovery import retry_stuck_paper_exits
from trading.runtime.paper_runner import PaperRunner
from trading.storage.trading_store import TradingEventType, TradingStore

CYCLE_NOW = f.NOW + timedelta(seconds=60)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(CYCLE_NOW)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


class TestAtomicBrokerPersistence:
    def test_submit_persists_broker_state_atomically(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        """Invariant 8: every fill mutation is durably mirrored before the next poll."""
        state_path = tmp_path / "broker_state.json"
        ids = SequentialIdFactory(clock.instant)
        broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES,
            clock=clock,
            id_factory=ids,
        )
        broker.bind_state_path(state_path)
        request = _submit_request()
        broker.submit(request)
        assert state_path.is_file()
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        assert payload["positions"]
        assert payload["orders"]
        assert not state_path.with_suffix(".json.tmp").exists()

    def test_atomic_write_uses_temp_file(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "broker_state.json"
        atomic_write_json(target, {"funds": {}, "positions": [], "orders": []})
        assert target.is_file()
        assert not target.with_suffix(".json.tmp").exists()


class TestStartupRepairFromFills:
    def test_repair_restores_missing_broker_legs_before_recovery(
        self, store: TradingStore, clock: FrozenClock, tmp_path: Path
    ) -> None:
        """Crash after fills but before broker_state write must not block entries."""
        runner = _open_discovery_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        trade_id = opened.trade_id
        filled_orders = [
            event
            for stored in store.read_events()
            if stored.event_type is TradingEventType.ORDER_EVENT
            for event in (stored.deserialize(),)
            if isinstance(event, OrderEvent)
            and event.identity.trade_id == trade_id
            and event.state is OrderState.FILLED
        ]
        assert filled_orders

        empty_broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES,
            clock=clock,
            id_factory=SequentialIdFactory(clock.instant),
        )
        state_path = tmp_path / "broker_state.json"
        atomic_write_json(state_path, empty_broker.dump_state())
        empty_broker.bind_state_path(state_path)

        repair_ids = SequentialIdFactory(clock.instant + timedelta(hours=1))
        events = repair_paper_broker_from_store(
            empty_broker,
            store,
            id_factory=repair_ids,
            clock=clock,
        )
        assert len(events) == 1
        assert events[0].repair_succeeded is True
        assert events[0].entries_blocked is False

        broker_keys = {
            (position.contract.symbol, position.side, position.quantity_contracts)
            for position in empty_broker.get_positions()
            if position.trade_id == trade_id
        }
        lifecycle = store.get_position_lifecycle(trade_id)
        assert lifecycle is not None
        local_keys = {
            (leg.contract.symbol, leg.side, leg.quantity_contracts)
            for leg in lifecycle.position.legs
        }
        assert broker_keys == local_keys

        restarted = PaperRunner(
            account_config=_paper_config(),  # type: ignore[arg-type]
            risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
            store=store,
            broker=empty_broker,
            clock=clock,
            id_factory=SequentialIdFactory(clock.instant),
        )
        recovery = restarted.recover_lifecycle()
        assert trade_id in recovery.restored_trade_ids
        assert recovery.entries_blocked is False
        assert not any(
            alert.reason_code is ReasonCode.UNRECONCILED_POSITION
            for alert in recovery.alerts
        )


class TestRetryStuckSelection:
    def test_healthy_open_trade_not_selected_without_trade_id(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        """OPEN trades with matching broker state are not stuck-exit candidates."""
        runner = _open_discovery_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        symbol = opened.legs[0].contract.symbol
        session_root, quotes_path = _persist_repo_for_retry(
            runner,
            store,
            tmp_path,
            quotes={symbol: {"bid": "1.00", "ask": "1.05", "last": "1.00"}},
        )

        result = retry_stuck_paper_exits(
            session_root,
            quotes_json=quotes_path,
            clock=clock,
            dry_run=True,
        )
        assert opened.trade_id not in result.trade_ids
        assert "0 trade(s)" in result.detail

    def test_trade_id_filter_targets_one_trade_even_when_healthy(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        runner = _open_discovery_long(store, clock)
        opened = runner.trade_manager.list_positions()[0]
        symbol = opened.legs[0].contract.symbol
        session_root, quotes_path = _persist_repo_for_retry(
            runner,
            store,
            tmp_path,
            quotes={symbol: {"bid": "1.00", "ask": "1.05", "last": "1.00"}},
        )

        result = retry_stuck_paper_exits(
            session_root,
            quotes_json=quotes_path,
            trade_id=opened.trade_id,
            clock=clock,
            dry_run=True,
            verbose=True,
        )
        assert opened.trade_id in result.verbose_log[0]
        assert "1 trade(s)" in result.detail
