"""DISC-A20: PAPER protection monitor quote delivery (WS + REST fallback)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Protocol, runtime_checkable

import pytest

import tests.factories as f
from tests.test_paper_lifecycle import _open_long
from tests.test_paper_runner import ROOT
from trading.data.settings import FyersSettings
from trading.domain.clock import FrozenClock
from trading.domain.contracts import DerivativesContext, FeatureSnapshot
from trading.domain.contracts.protection import ProtectionHeartbeat
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import DataQuality, OptionType
from trading.domain.primitives import Price, TickSize
from trading.runtime.fyers_ws_monitor import FyersWsQuoteMonitor, _default_tick_quote
from trading.runtime.protection import (
    ProtectionCoordinator,
    build_protection_coordinator,
)
from trading.runtime.rest_quote_monitor import RestQuoteMonitor, RestQuoteRateLimitError
from trading.storage.trading_store import TradingStore

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)


def _fyers_settings() -> FyersSettings:
    return FyersSettings.model_construct(
        fyers_app_id="app",
        fyers_secret_key="test-secret",
        fyers_access_token="test-token",
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW + timedelta(seconds=60))


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _option_snapshot(
    contract: object,
    *,
    bid: str,
    ask: str,
    now: datetime,
) -> FeatureSnapshot:
    calc = now - timedelta(seconds=1)
    times = f.snapshot_times(
        event_time=calc,
        source_time=calc,
        receive_time=calc + timedelta(milliseconds=50),
        calculation_time=calc + timedelta(milliseconds=120),
    )
    return f.snapshot(
        contract=contract,
        market=f.quote(bid=f.price(bid), ask=f.price(ask), last=f.price(bid)),
        times=times,
        quality=f.quality(state=DataQuality.VALID),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    )


@runtime_checkable
class _WsSocket(Protocol):
    def connect(self) -> None: ...

    def subscribe(
        self,
        symbols: list[str],
        data_type: str = "SymbolUpdate",
        channel: int = 11,
    ) -> None: ...

    def keep_running(self) -> None: ...

    def close_connection(self) -> None: ...


class TestProtectionHandlerWiring:
    def test_ws_handler_wired_to_coordinator(
        self, store: TradingStore, clock: FrozenClock, tmp_path: Path
    ) -> None:
        runner = _open_long(store, clock)
        position = runner.trade_manager.list_positions()[0]
        symbol = position.legs[0].contract.symbol

        class FakeSocket:
            on_message: object

            def connect(self) -> None:
                on_message = self.on_message
                if callable(on_message):
                    on_message(
                        {
                            "symbol": symbol,
                            "ltp": 91.95,
                            "bid_price": 91.90,
                            "ask_price": 92.00,
                        }
                    )

            def subscribe(
                self,
                symbols: list[str],
                data_type: str = "SymbolUpdate",
                channel: int = 11,
            ) -> None:
                assert symbol in symbols

            def keep_running(self) -> None:
                return None

            def close_connection(self) -> None:
                return None

        def socket_factory(**kwargs: object) -> FakeSocket:
            socket = FakeSocket()
            socket.on_message = kwargs["on_message"]
            return socket

        ws = FyersWsQuoteMonitor(
            _fyers_settings(),
            clock,
            tmp_path,
            socket_factory=socket_factory,
            sleeper=lambda _: None,
            reconnect_backoff_seconds=0.0,
        )
        from trading.runtime.paper_session import load_paper_session_config

        coordinator = build_protection_coordinator(
            runner=runner,
            clock=clock,
            config=load_paper_session_config(
                ROOT / "config" / "paper_session.yaml"
            ).protection,
            repo_root=tmp_path,
            ws=ws,
        )
        snap = _option_snapshot(
            position.legs[0].contract,
            bid="91.95",
            ask="92.00",
            now=clock.now_utc(),
        )
        coordinator.seed_snapshots({symbol: snap})
        coordinator.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and coordinator._last_quote_at is None:
            time.sleep(0.01)
        assert coordinator._last_quote_at is not None
        coordinator.stop()


class TestWsQuoteParsing:
    def test_default_tick_quote_maps_fyers_ws_fields(self) -> None:
        quote = _default_tick_quote(
            "NSE:TEST",
            {"ltp": 100.5, "bid_price": 100.0, "ask_price": 101.0},
            NOW,
        )
        assert quote is not None
        assert quote.last == Price.snap("100.50", TickSize.of(Decimal("0.05")))
        assert quote.bid == Price.snap("100.00", TickSize.of(Decimal("0.05")))
        assert quote.ask == Price.snap("101.00", TickSize.of(Decimal("0.05")))


class TestWsConnectedSemantics:
    def test_not_connected_until_first_tick(
        self, store: TradingStore, clock: FrozenClock, tmp_path: Path
    ) -> None:
        runner = _open_long(store, clock)
        symbol = runner.trade_manager.list_positions()[0].legs[0].contract.symbol
        blocked = {"release": False}

        class WaitingSocket:
            def connect(self) -> None:
                return None

            def subscribe(
                self,
                symbols: list[str],
                data_type: str = "SymbolUpdate",
                channel: int = 11,
            ) -> None:
                assert symbol in symbols

            def keep_running(self) -> None:
                while not blocked["release"]:
                    pass

            def close_connection(self) -> None:
                blocked["release"] = True

        ws = FyersWsQuoteMonitor(
            _fyers_settings(),
            clock,
            tmp_path,
            socket_factory=lambda **_kwargs: WaitingSocket(),
            sleeper=lambda _: None,
            reconnect_backoff_seconds=0.0,
        )
        from trading.runtime.paper_session import load_paper_session_config

        coordinator = build_protection_coordinator(
            runner=runner,
            clock=clock,
            config=load_paper_session_config(
                ROOT / "config" / "paper_session.yaml"
            ).protection,
            repo_root=tmp_path,
            ws=ws,
        )
        coordinator.start()
        assert ws.ws_connected is False
        blocked["release"] = True
        coordinator.stop()
        assert ws.ws_connected is False

    def test_connected_after_first_tick(
        self, store: TradingStore, clock: FrozenClock, tmp_path: Path
    ) -> None:
        runner = _open_long(store, clock)
        symbol = runner.trade_manager.list_positions()[0].legs[0].contract.symbol

        class TickingSocket:
            on_message: object

            def connect(self) -> None:
                on_message = self.on_message
                if callable(on_message):
                    on_message(
                        {
                            "symbol": symbol,
                            "ltp": 92.0,
                            "bid_price": 91.9,
                            "ask_price": 92.1,
                        }
                    )

            def subscribe(
                self,
                symbols: list[str],
                data_type: str = "SymbolUpdate",
                channel: int = 11,
            ) -> None:
                return None

            def keep_running(self) -> None:
                return None

            def close_connection(self) -> None:
                return None

        def socket_factory(**kwargs: object) -> TickingSocket:
            socket = TickingSocket()
            socket.on_message = kwargs["on_message"]
            return socket

        ws = FyersWsQuoteMonitor(
            _fyers_settings(),
            clock,
            tmp_path,
            socket_factory=socket_factory,
            sleeper=lambda _: None,
            reconnect_backoff_seconds=0.0,
        )
        from trading.runtime.paper_session import load_paper_session_config

        coordinator = build_protection_coordinator(
            runner=runner,
            clock=clock,
            config=load_paper_session_config(
                ROOT / "config" / "paper_session.yaml"
            ).protection,
            repo_root=tmp_path,
            ws=ws,
        )
        coordinator.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not ws.ws_connected:
            time.sleep(0.01)
        assert ws.ws_connected is True
        coordinator.stop()


class TestRestFallback:
    def test_rest_fetch_invokes_on_quote_update(
        self, store: TradingStore, clock: FrozenClock, tmp_path: Path
    ) -> None:
        runner = _open_long(store, clock)
        position = runner.trade_manager.list_positions()[0]
        symbol = position.legs[0].contract.symbol
        calls: list[tuple[str, ...]] = []

        def fetch(symbols: tuple[str, ...]) -> dict[str, MarketQuote]:
            calls.append(symbols)
            return {
                symbol: MarketQuote(
                    last=Price.snap("92.00", TickSize.of(Decimal("0.05"))),
                    bid=Price.snap("91.95", TickSize.of(Decimal("0.05"))),
                    ask=Price.snap("92.05", TickSize.of(Decimal("0.05"))),
                )
            }

        rest = RestQuoteMonitor(clock, fetch, poll_seconds=1)
        from trading.runtime.paper_session import load_paper_session_config

        coordinator = build_protection_coordinator(
            runner=runner,
            clock=clock,
            config=load_paper_session_config(
                ROOT / "config" / "paper_session.yaml"
            ).protection,
            repo_root=tmp_path,
            rest_fetch=rest,
        )
        snap = _option_snapshot(
            position.legs[0].contract,
            bid="91.95",
            ask="92.00",
            now=clock.now_utc(),
        )
        coordinator.seed_snapshots({symbol: snap})
        coordinator.start()
        coordinator.tick()
        assert calls == [(symbol,)]
        assert coordinator._last_quote_at is not None
        coordinator.stop()

    def test_rest_poll_respects_interval(self, clock: FrozenClock) -> None:
        calls = {"n": 0}

        def fetch(_symbols: tuple[str, ...]) -> dict[str, MarketQuote]:
            calls["n"] += 1
            return {}

        rest = RestQuoteMonitor(clock, fetch, poll_seconds=5)
        rest.subscribe(frozenset({"NSE:TEST"}))
        rest.start()
        rest.set_handler(lambda *_args: None)
        rest.tick()
        rest.tick()
        assert calls["n"] == 1

    def test_rest_backs_off_on_rate_limit(self, clock: FrozenClock) -> None:
        calls = {"n": 0}

        def fetch(_symbols: tuple[str, ...]) -> dict[str, MarketQuote]:
            calls["n"] += 1
            raise RestQuoteRateLimitError("429")

        rest = RestQuoteMonitor(clock, fetch, poll_seconds=1)
        rest.subscribe(frozenset({"NSE:TEST"}))
        rest.start()
        rest.set_handler(lambda *_args: None)
        rest.tick()
        rest.tick()
        assert calls["n"] == 1


class TestProtectionHeartbeat:
    def test_last_quote_at_written_on_ws_quote(
        self, store: TradingStore, clock: FrozenClock, tmp_path: Path
    ) -> None:
        runner = _open_long(store, clock)
        symbol = runner.trade_manager.list_positions()[0].legs[0].contract.symbol

        class TickingSocket:
            on_message: object

            def connect(self) -> None:
                on_message = self.on_message
                if callable(on_message):
                    on_message(
                        {
                            "symbol": symbol,
                            "ltp": 92.0,
                            "bid_price": 91.9,
                            "ask_price": 92.1,
                        }
                    )

            def subscribe(
                self,
                symbols: list[str],
                data_type: str = "SymbolUpdate",
                channel: int = 11,
            ) -> None:
                return None

            def keep_running(self) -> None:
                return None

            def close_connection(self) -> None:
                return None

        def socket_factory(**kwargs: object) -> TickingSocket:
            socket = TickingSocket()
            socket.on_message = kwargs["on_message"]
            return socket

        ws = FyersWsQuoteMonitor(
            _fyers_settings(),
            clock,
            tmp_path,
            socket_factory=socket_factory,
            sleeper=lambda _: None,
            reconnect_backoff_seconds=0.0,
        )
        from trading.runtime.paper_session import load_paper_session_config

        config = load_paper_session_config(
            ROOT / "config" / "paper_session.yaml"
        ).protection
        coordinator: ProtectionCoordinator = build_protection_coordinator(
            runner=runner,
            clock=clock,
            config=config,
            repo_root=tmp_path,
            ws=ws,
        )
        position = runner.trade_manager.list_positions()[0]
        snap = _option_snapshot(
            position.legs[0].contract,
            bid="91.95",
            ask="92.00",
            now=clock.now_utc(),
        )
        coordinator.seed_snapshots({symbol: snap})
        coordinator.start()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and coordinator._last_quote_at is None:
            time.sleep(0.01)
        heartbeat_path = tmp_path / config.heartbeat_path
        heartbeat = ProtectionHeartbeat.model_validate_json(
            heartbeat_path.read_text(encoding="utf-8")
        )
        assert coordinator._last_quote_at is not None
        assert heartbeat.last_quote_at is not None
        assert heartbeat.ws_connected is True
        coordinator.stop()
