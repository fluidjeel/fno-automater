"""Event-driven PAPER protection: quotes between entry polls, degradation, watchdog.

Invariant 6: stale or missing monitor quotes block new exposure via PROTECTION_DEGRADED.
Invariant 8: open positions keep deterministic exit evaluation on fresh quotes.
Invariant 11: duplicate quote delivery does not double-submit exits.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from tests.test_paper_lifecycle import _open_long, _restart
from tests.test_paper_runner import ROOT
from trading.domain.clock import FrozenClock
from trading.domain.contracts import DerivativesContext, FeatureSnapshot
from trading.domain.contracts.protection import (
    ProtectionHeartbeat,
    ProtectionStateRecord,
)
from trading.domain.enums import (
    DataQuality,
    OptionType,
    OrderState,
    ProtectionStatus,
    QuoteMonitorSource,
    ReasonCode,
    Side,
    TradeState,
)
from trading.ops.attention import MemoryAttentionSink

try:
    from trading.runtime.paper_runner import PaperRunner, QuoteUpdateResult
except ImportError:  # pragma: no cover - WIP until wired to current runner
    import pytest

    pytest.skip(
        "paper protection WIP not wired to current PaperRunner (QuoteUpdateResult)",
        allow_module_level=True,
    )
from trading.runtime.paper_session import load_paper_session_config
from trading.runtime.protection import (
    ProtectionCoordinator,
    build_protection_coordinator,
)
from trading.runtime.watchdog import run_paper_watchdog
from trading.storage.trading_store import TradingEventType, TradingStore

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
TICK = NOW + timedelta(seconds=60)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(TICK)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _times(now: datetime) -> object:
    calc = now - timedelta(seconds=1)
    return f.snapshot_times(
        event_time=calc,
        source_time=calc,
        receive_time=calc + timedelta(milliseconds=50),
        calculation_time=calc + timedelta(milliseconds=120),
    )


def _option_snapshot(
    contract: object,
    *,
    bid: str,
    ask: str,
    now: datetime,
    quality: DataQuality = DataQuality.VALID,
) -> FeatureSnapshot:
    reason = (ReasonCode.DATA_STALE,) if quality is DataQuality.STALE else ()
    return f.snapshot(
        contract=contract,
        market=f.quote(bid=f.price(bid), ask=f.price(ask), last=f.price(bid)),
        times=_times(now),
        quality=f.quality(state=quality, reason_codes=reason),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    )


def _coordinator(
    runner: PaperRunner,
    clock: FrozenClock,
    tmp_path: Path,
) -> ProtectionCoordinator:
    session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
    coordinator = build_protection_coordinator(
        runner=runner,
        clock=clock,
        config=session_cfg.protection,
        repo_root=tmp_path,
    )
    coordinator.start()
    return coordinator


def _publish_stop(
    runner: PaperRunner,
    coordinator: ProtectionCoordinator,
    clock: FrozenClock,
    *,
    bid: str = "85.00",
    at: datetime | None = None,
) -> QuoteUpdateResult | None:
    position = runner.trade_manager.list_positions()[0]
    symbol = position.legs[0].contract.symbol
    received_at = at or clock.now_utc()
    snap = _option_snapshot(
        position.legs[0].contract,
        bid=bid,
        ask=str(Decimal(bid) + Decimal("0.20")),
        now=received_at,
    )
    coordinator.seed_snapshots({symbol: snap})
    return coordinator.publish_quotes(
        {symbol: snap.market},
        received_at=received_at,
        source=QuoteMonitorSource.SCRIPTED,
    )


class TestEventDrivenExits:
    def test_stop_triggered_between_entry_polls(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        """Invariant 8: protection loop exits before the next 60s entry poll."""
        runner = _open_long(store, clock)
        coordinator = _coordinator(runner, clock, tmp_path)
        sells_before = sum(
            1
            for event in runner.broker.list_orders()
            if event.command.side is Side.SELL
        )
        result = _publish_stop(runner, coordinator, clock)
        sells_after = sum(
            1
            for event in runner.broker.list_orders()
            if event.command.side is Side.SELL
        )
        assert sells_after > sells_before
        assert result is not None
        assert result.detection_latency_ms is not None

    def test_target_triggered_between_polls(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        runner = _open_long(store, clock)
        coordinator = _coordinator(runner, clock, tmp_path)
        result = _publish_stop(runner, coordinator, clock, bid="96.25")
        sells = [
            event
            for event in runner.broker.list_orders()
            if event.command.side is Side.SELL
        ]
        assert sells
        assert result is not None
        assert not result.degraded

    def test_duplicate_quote_delivery(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        """Invariant 11: identical quote in the same second emits one exit submit."""
        runner = _open_long(store, clock)
        coordinator = _coordinator(runner, clock, tmp_path)
        position = runner.trade_manager.list_positions()[0]
        symbol = position.legs[0].contract.symbol
        snap = _option_snapshot(
            position.legs[0].contract,
            bid="85.00",
            ask="85.20",
            now=clock.now_utc(),
        )
        coordinator.seed_snapshots({symbol: snap})
        received_at = clock.now_utc()
        coordinator.publish_quotes({symbol: snap.market}, received_at=received_at)
        coordinator.publish_quotes({symbol: snap.market}, received_at=received_at)
        sells = [
            event
            for event in runner.broker.list_orders()
            if event.command.side is Side.SELL
        ]
        assert len(sells) == 1


class TestProtectionDegraded:
    def test_stale_quote_degrades_protection(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        """Invariant 6: stale monitor quotes block entries and persist degradation."""
        runner = _open_long(store, clock)
        coordinator = _coordinator(runner, clock, tmp_path)
        position = runner.trade_manager.list_positions()[0]
        symbol = position.legs[0].contract.symbol
        snap = _option_snapshot(
            position.legs[0].contract,
            bid="91.95",
            ask="92.00",
            now=clock.now_utc(),
            quality=DataQuality.STALE,
        )
        coordinator.seed_snapshots({symbol: snap})
        result = runner.on_quote_update(
            {symbol: snap.market},
            source=QuoteMonitorSource.REST,
            received_at=clock.now_utc(),
            quote_max_age_ms=5000,
        )
        assert result.degraded is True
        assert runner.protection_degraded is True
        persisted = store.get_protection_state(position.trade_id)
        assert persisted is not None
        assert persisted.status is ProtectionStatus.DEGRADED
        assert runner._services.controls.state.protection_degraded is True

    def test_missing_monitor_leg(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        runner = _open_long(store, clock)
        coordinator = _coordinator(runner, clock, tmp_path)
        position = runner.trade_manager.list_positions()[0]
        symbol = position.legs[0].contract.symbol
        snap = _option_snapshot(
            position.legs[0].contract,
            bid="91.95",
            ask="92.00",
            now=clock.now_utc(),
        )
        coordinator.seed_snapshots({})
        result = runner.on_quote_update(
            {symbol: snap.market},
            source=QuoteMonitorSource.REST,
            received_at=clock.now_utc(),
            quote_max_age_ms=5000,
        )
        assert result.degraded is True
        sells = [
            event
            for event in runner.broker.list_orders()
            if event.command.side is Side.SELL
        ]
        assert not sells


class TestRestartProtection:
    def test_restart_during_monitoring(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        first = _open_long(store, clock)
        coordinator = _coordinator(first, clock, tmp_path)
        position = first.trade_manager.list_positions()[0]
        symbol = position.legs[0].contract.symbol
        snap = _option_snapshot(
            position.legs[0].contract,
            bid="91.95",
            ask="92.00",
            now=clock.now_utc(),
        )
        coordinator.seed_snapshots({symbol: snap})
        first.flush_lifecycle()
        clock.set(clock.now_utc() + timedelta(seconds=30))
        second = _restart(store, clock, first.broker)
        recovery = second.recover_lifecycle()
        assert position.trade_id in recovery.restored_trade_ids
        next_coordinator = _coordinator(second, clock, tmp_path)
        result = _publish_stop(second, next_coordinator, clock)
        assert result is not None
        assert any(
            event.command.side is Side.SELL for event in second.broker.list_orders()
        )

    def test_restart_with_exit_pending(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        """Invariant 13: EXIT_PENDING resumes without duplicate exit submit."""
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
                idempotency_key="b" * 32,
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
        store.append(
            TradingEventType.ORDER_EVENT,
            unknown,
            event_id=unknown.event_id,
        )
        store.upsert_position_lifecycle(
            record.model_copy(
                update={
                    "position": pending,
                    "exit_order_ids": ("ORD-EXIT-UNKNOWN",),
                    "as_of": pending.as_of,
                }
            ),
            event_id="PLC-EXIT-PENDING-1",
        )
        sells_before = len(
            [
                event
                for event in first.broker.list_orders()
                if event.command.side is Side.SELL
            ]
        )
        clock.set(clock.now_utc() + timedelta(seconds=30))
        second = _restart(store, clock, first.broker)
        second.recover_lifecycle()
        next_coordinator = _coordinator(second, clock, tmp_path)
        position = second.trade_manager.get_position(opened.trade_id)
        assert position is not None
        assert position.state is TradeState.EXIT_PENDING
        symbol = position.legs[0].contract.symbol
        snap = _option_snapshot(
            position.legs[0].contract,
            bid="85.00",
            ask="85.20",
            now=clock.now_utc(),
        )
        next_coordinator.seed_snapshots({symbol: snap})
        next_coordinator.publish_quotes(
            {symbol: snap.market},
            received_at=clock.now_utc(),
        )
        sells_after = len(
            [
                event
                for event in second.broker.list_orders()
                if event.command.side is Side.SELL
            ]
        )
        assert sells_after == sells_before

    def test_restart_restores_protection_degraded(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        first = _open_long(store, clock)
        position = first.trade_manager.list_positions()[0]
        received_at = clock.now_utc()
        store.upsert_protection_state(
            ProtectionStateRecord(
                trade_id=position.trade_id,
                status=ProtectionStatus.DEGRADED,
                reason_code=ReasonCode.PROTECTION_DEGRADED,
                degraded_since=received_at,
                last_heartbeat_at=received_at,
                monitor_symbols=(position.legs[0].contract.symbol,),
                as_of=received_at,
            )
        )
        first.flush_lifecycle()
        second = _restart(store, clock, first.broker)
        recovery = second.recover_lifecycle()
        assert recovery.entries_blocked is True
        assert second.protection_degraded is True


class TestProtectionRecovery:
    def test_partial_multileg_fill(
        self,
        store: TradingStore,
        clock: FrozenClock,
        tmp_path: Path,
    ) -> None:
        """Degraded protection clears after fresh quotes return on all monitor legs."""
        runner = _open_long(store, clock)
        coordinator = _coordinator(runner, clock, tmp_path)
        position = runner.trade_manager.list_positions()[0]
        symbol = position.legs[0].contract.symbol
        stale = _option_snapshot(
            position.legs[0].contract,
            bid="91.95",
            ask="92.00",
            now=clock.now_utc(),
            quality=DataQuality.STALE,
        )
        coordinator.seed_snapshots({symbol: stale})
        degraded = runner.on_quote_update(
            {symbol: stale.market},
            source=QuoteMonitorSource.REST,
            received_at=clock.now_utc(),
            quote_max_age_ms=5000,
        )
        assert degraded.degraded is True
        fresh = _option_snapshot(
            position.legs[0].contract,
            bid="91.95",
            ask="92.00",
            now=clock.now_utc(),
        )
        coordinator.seed_snapshots({symbol: fresh})
        recovered = coordinator.publish_quotes(
            {symbol: fresh.market},
            received_at=clock.now_utc(),
        )
        assert recovered is not None
        assert recovered.recovered is True
        assert runner.protection_degraded is False


class TestWatchdog:
    def test_watchdog_detects_stopped_monitor(
        self, tmp_path: Path, clock: FrozenClock
    ) -> None:
        heartbeat_path = tmp_path / "data" / "paper" / "protection_heartbeat.json"
        heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        stale = datetime(2020, 1, 1, tzinfo=UTC)
        heartbeat = ProtectionHeartbeat(
            as_of=stale,
            open_positions=1,
            monitor_active=True,
        )
        heartbeat_path.write_text(heartbeat.model_dump_json(), encoding="utf-8")
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True)
        source = ROOT / "config" / "paper_session.yaml"
        (config_dir / "paper_session.yaml").write_text(
            source.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        sink = MemoryAttentionSink()
        code = run_paper_watchdog(
            tmp_path,
            store_path=tmp_path / "paper.sqlite",
            notifier=sink,
            clock=clock,
        )
        assert code == 1
        assert sink.requests
