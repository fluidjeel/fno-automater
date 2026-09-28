"""DISC-A35: PAPER protection perf — deferred work, audit index, EOD while degraded."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import tests.factories as f
from tests.test_paper_lifecycle import _open_long
from tests.test_paper_runner import ROOT, _paper_config
from trading.broker.paper import PaperBroker
from trading.config import load_evaluation_config, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import ReasonCode
from trading.domain.ids import SequentialIdFactory
from trading.domain.primitives import Price, TickSize
from trading.runtime.paper_runner import LifecycleAlert, PaperRunner
from trading.runtime.paper_session import PaperSession, load_paper_session_config
from trading.runtime.protection import build_protection_coordinator
from trading.runtime.rest_quote_monitor import RestQuoteMonitor
from trading.storage.trading_store import TradingEventType, TradingStore

IST = ZoneInfo("Asia/Kolkata")
NOW = f.NOW + timedelta(seconds=60)
TICK = Decimal("0.05")


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


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


def _runner(store: TradingStore, clock: FrozenClock) -> PaperRunner:
    ids = SequentialIdFactory(clock.instant)
    fill_model = load_evaluation_config(
        ROOT / "config" / "evaluation.yaml"
    ).config.fill_model
    broker = PaperBroker.from_fixtures(
        Path(__file__).resolve().parent / "fixtures" / "broker",
        clock=clock,
        id_factory=ids,
        fill_model=fill_model,
    )
    return PaperRunner(
        account_config=_paper_config(),  # type: ignore[arg-type]
        risk_policy=load_risk_policy(ROOT / "config" / "risk.yaml"),
        store=store,
        broker=broker,
        clock=clock,
        id_factory=ids,
        fill_model=fill_model,
    )


def _quote(symbol: str, *, bid: str = "90.00") -> MarketQuote:
    return MarketQuote(
        last=Price.snap(bid, TickSize.of(TICK)),
        bid=Price.snap(bid, TickSize.of(TICK)),
        ask=Price.snap(str(Decimal(bid) + Decimal("0.05")), TickSize.of(TICK)),
    )


class TestProtectionDeferredWork:
    def test_rest_tick_with_many_symbols_completes_quickly_and_builds_once(
        self, store: TradingStore, clock: FrozenClock, tmp_path: Path
    ) -> None:
        """Invariant 8: one REST batch must not invoke the session builder per quote."""
        runner = _open_long(store, clock)
        position = runner.trade_manager.list_positions()[0]
        monitor_symbol = position.legs[0].contract.symbol
        all_symbols = (
            monitor_symbol,
            *(f"NSE:NIFTY26SEP24{i:03d}CE" for i in range(29)),
        )
        builder_calls = {"count": 0}

        def builder(_now: datetime) -> tuple[tuple[object, ...], dict[str, object]]:
            builder_calls["count"] += 1
            time.sleep(0.01)
            return (), {}

        def fetch(symbols: tuple[str, ...]) -> dict[str, MarketQuote]:
            return {symbol: _quote(symbol) for symbol in symbols}

        session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
        protection_cfg = session_cfg.protection.model_copy(
            update={"deferred_work_floor_seconds": 1, "tick_budget_seconds": 5.0}
        )
        rest = RestQuoteMonitor(clock, fetch, poll_seconds=1)
        coordinator = build_protection_coordinator(
            runner=runner,
            clock=clock,
            config=protection_cfg,
            repo_root=tmp_path,
            rest_fetch=rest,
        )
        session = PaperSession(
            runner=runner,
            clock=clock,
            session_config=session_cfg,
            session_hours=(dt_time(9, 15), dt_time(15, 30)),
            timezone=IST,
            notifier=_Sink(),
            request_builder=builder,  # type: ignore[arg-type]
            observation_start=NOW,
            capital_limit=f.money("700000"),
            risk_policy_version="4",
            fill_model_version="conservative-v1",
            code_version="disc-a35",
            charges_verified=False,
            cohort_dir=tmp_path / "cohorts",
        )
        coordinator.set_m1_quote_handler(
            lambda symbol, quote, received_at: session.on_provider_quote(
                symbol, quote, receive_time=received_at
            )
        )
        coordinator.start()
        rest.subscribe(frozenset(all_symbols))
        started = time.monotonic()
        coordinator.tick()
        elapsed = time.monotonic() - started
        coordinator.stop()
        assert elapsed < 2.0
        assert builder_calls["count"] <= 1

    def test_manage_exits_audit_uses_index_not_full_scan(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        """Lifecycle audit must not scan the full event log on every exit pass."""
        runner = _runner(store, clock)
        runner._seed_unresolved_lifecycle_index()
        read_calls = {"count": 0}
        original = store.read_events

        def counting_read_events(*args: object, **kwargs: object) -> tuple[object, ...]:
            read_calls["count"] += 1
            return original(*args, **kwargs)

        alerts = tuple(
            LifecycleAlert(
                trade_id=f"TRD-PERF-{index}",
                reason_code=ReasonCode.PROTECTION_DEGRADED,
                detail="stale quotes",
            )
            for index in range(21)
        )
        with patch.object(store, "read_events", side_effect=counting_read_events):
            runner._audit_lifecycle_alerts(alerts)
        assert read_calls["count"] == 0

    def test_audit_skips_already_open_alerts(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        runner = _runner(store, clock)
        runner.recover_lifecycle()
        alert = LifecycleAlert(
            trade_id="TRD-TEST-1",
            reason_code=ReasonCode.PROTECTION_DEGRADED,
            detail="stale quotes",
        )
        runner._audit_lifecycle_alerts((alert,))
        before = len(
            [
                stored
                for stored in store.read_events()
                if stored.event_type is TradingEventType.RECONCILIATION_EVENT
            ]
        )
        runner._audit_lifecycle_alerts((alert,))
        after = len(
            [
                stored
                for stored in store.read_events()
                if stored.event_type is TradingEventType.RECONCILIATION_EVENT
            ]
        )
        assert after == before


class TestEodWhileDegraded:
    def test_session_exits_at_eod_while_protection_degraded(
        self, store: TradingStore, tmp_path: Path
    ) -> None:
        """Invariant 8: EOD must fire even when protection is stuck on stale quotes."""
        session_start = datetime(2026, 9, 14, 10, 9, 30, tzinfo=UTC)
        clock = FrozenClock(NOW)
        runner = _open_long(store, clock)
        clock.advance(session_start - NOW)
        position = runner.trade_manager.list_positions()[0]
        monitor_symbol = position.legs[0].contract.symbol
        builder_calls = {"count": 0}

        def slow_builder(
            _now: datetime,
        ) -> tuple[tuple[object, ...], dict[str, object]]:
            builder_calls["count"] += 1
            time.sleep(0.05)
            return (), dict(runner.protection_snapshots)

        def fetch(symbols: tuple[str, ...]) -> dict[str, MarketQuote]:
            return {symbol: _quote(symbol) for symbol in symbols}

        session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
        session_cfg = session_cfg.model_copy(update={"eod_local": "15:40"})
        protection_cfg = session_cfg.protection.model_copy(
            update={
                "deferred_work_floor_seconds": 1,
                "tick_budget_seconds": 0.05,
                "rest_poll_seconds": 1,
            }
        )
        rest = RestQuoteMonitor(clock, fetch, poll_seconds=1)
        protection = build_protection_coordinator(
            runner=runner,
            clock=clock,
            config=protection_cfg,
            repo_root=tmp_path,
            rest_fetch=rest,
        )
        sink = _Sink()
        session = PaperSession(
            runner=runner,
            clock=clock,
            session_config=session_cfg,
            session_hours=(dt_time(9, 15), dt_time(15, 30)),
            timezone=IST,
            notifier=sink,
            request_builder=slow_builder,  # type: ignore[arg-type]
            observation_start=session_start,
            capital_limit=f.money("700000"),
            risk_policy_version="4",
            fill_model_version="conservative-v1",
            code_version="disc-a35",
            charges_verified=False,
            cohort_dir=tmp_path / "cohorts",
            protection=protection,
            sleeper=lambda _seconds: clock.advance(timedelta(seconds=1)),
        )
        protection.set_m1_quote_handler(
            lambda symbol, quote, received_at: session.on_provider_quote(
                symbol, quote, receive_time=received_at
            )
        )
        protection.start()
        rest.subscribe(frozenset({monitor_symbol}))
        for _ in range(45):
            if session._should_stop_session():
                session._runner.flush_lifecycle()
                session._runner.broker.persist_state()
                session._send_eod(clock.now_utc())
                break
            session._write_session_heartbeat(
                now=clock.now_utc(), result=None, had_requests=False
            )
            protection.refresh_subscriptions()
            protection.tick(should_stop=session._should_stop_session)
            clock.advance(timedelta(seconds=1))
        protection.stop()
        assert session._should_stop_session()
        assert any("PAPER EOD" in message for message in sink.messages)
