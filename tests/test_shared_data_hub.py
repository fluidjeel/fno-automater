"""Shared Fyers data_ws hub: single socket, refcounting, and fallback behavior."""

from __future__ import annotations

import time
from collections.abc import Generator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from trading.data.fyers.data_socket_guard import (
    DataSocketAlreadyOpenError,
    DataSocketGuard,
    is_data_socket_locked,
    process_has_data_socket,
    set_process_data_socket_open,
)
from trading.data.fyers.shared_hub.client import SharedDataSocketClient, SharedHubClient
from trading.data.fyers.shared_hub.server import SharedDataHubServer
from trading.data.fyers.shared_hub.subscriptions import SubscriptionRegistry
from trading.data.fyers.ws import FyersTickStream
from trading.data.settings import FyersSettings
from trading.domain.clock import FrozenClock
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import QuoteMonitorSource
from trading.domain.primitives import Price, TickSize
from trading.runtime.protection import ProtectionConfig, build_quote_monitor
from trading.runtime.rest_quote_monitor import RestQuoteMonitor

_FAKE_SOCKET_INSTANCES: list[Any] = []


class _FakeSocket:
    def __init__(self, **_kwargs: Any) -> None:
        self.subscriptions: list[tuple[list[str], str]] = []
        self.connected = False
        self.closed = False
        _FAKE_SOCKET_INSTANCES.append(self)

    def connect(self) -> None:
        self.connected = True

    def subscribe(
        self,
        symbols: list[str],
        data_type: str = "SymbolUpdate",
        channel: int = 11,
    ) -> None:
        self.subscriptions.append((symbols, data_type))

    def keep_running(self) -> None:
        while self.connected and not self.closed:
            time.sleep(0.05)

    def close_connection(self) -> None:
        self.closed = True
        self.connected = False


@pytest.fixture(autouse=True)
def _reset_socket_state() -> Generator[None, None, None]:
    _FAKE_SOCKET_INSTANCES.clear()
    set_process_data_socket_open(False)
    yield
    set_process_data_socket_open(False)


@pytest.fixture
def settings() -> FyersSettings:
    return _settings_stub()


def _settings_stub() -> FyersSettings:
    return MagicMock(spec=FyersSettings, auth_header="Bearer test")


def test_subscription_registry_refcounts_owner_tags() -> None:
    registry = SubscriptionRegistry()
    activated = registry.subscribe("protection", "NSE:ABC", "SymbolUpdate")
    assert len(activated) == 1
    assert registry.subscribe("depth", "NSE:ABC", "SymbolUpdate") == set()
    deactivated = registry.unsubscribe("protection", "NSE:ABC", "SymbolUpdate")
    assert deactivated == set()
    assert registry.active_keys()
    deactivated = registry.unsubscribe("depth", "NSE:ABC", "SymbolUpdate")
    assert len(deactivated) == 1
    assert not registry.active_keys()


def test_data_socket_guard_refuses_second_open(tmp_path: Path) -> None:
    first = DataSocketGuard.acquire(tmp_path, "fno-data-tick")
    assert is_data_socket_locked(tmp_path)
    with pytest.raises(DataSocketAlreadyOpenError):
        DataSocketGuard.acquire(tmp_path, "paper-protection")
    DataSocketGuard.release(first)
    assert not is_data_socket_locked(tmp_path)


def test_at_most_one_fyers_data_socket_constructed(
    tmp_path: Path,
    settings: FyersSettings,
) -> None:
    """Invariant: one guarded socket across tick daemon and hub consumers."""
    clock = FrozenClock(datetime(2026, 9, 28, 4, 0, tzinfo=UTC))
    hub = SharedDataHubServer(
        settings,
        tmp_path,
        owner="fno-data-tick",
        clock=clock,
        socket_factory=lambda **_kwargs: _FakeSocket(),
        sleep=time.sleep,
    )
    hub.start(initial_symbols=[("NSE:NIFTY50-INDEX", "SymbolUpdate")])
    time.sleep(0.1)
    with pytest.raises(DataSocketAlreadyOpenError):
        stream = FyersTickStream(
            settings,
            clock,
            MagicMock(),
            repo_root=tmp_path,
            normalization_version="1",
            socket_factory=lambda **_kwargs: _FakeSocket(),
            sleep=time.sleep,
        )
        stream.collect("NSE:NIFTY50-INDEX", max_ticks=1, duration_seconds=1)
    depth = SharedHubClient(
        tmp_path,
        owner="cas-depth",
        data_type="DepthUpdate",
        sleep=time.sleep,
    )
    depth.subscribe(frozenset({"MCX:CRUDEOILM25OCTFUT"}))
    depth.start()
    time.sleep(0.1)
    assert len(_FAKE_SOCKET_INSTANCES) == 1
    hub.stop()
    depth.stop()


def test_shared_hub_delivers_symbol_update_to_protection_client(tmp_path: Path) -> None:
    settings = _settings_stub()
    callbacks: list[Any] = []

    def socket_factory(**kwargs: Any) -> _FakeSocket:
        callbacks.append(kwargs["on_message"])
        return _FakeSocket()

    hub = SharedDataHubServer(
        settings,
        tmp_path,
        clock=FrozenClock(datetime(2026, 9, 28, 4, 0, tzinfo=UTC)),
        socket_factory=socket_factory,
        sleep=time.sleep,
    )
    hub.start(initial_symbols=[("NSE:OPT", "SymbolUpdate")])
    time.sleep(0.1)
    received: list[tuple[str, MarketQuote, QuoteMonitorSource]] = []

    def handler(
        symbol: str,
        quote: MarketQuote,
        source: QuoteMonitorSource,
        _received_at: datetime,
    ) -> None:
        received.append((symbol, quote, source))

    client = SharedDataSocketClient(
        tmp_path,
        clock=FrozenClock(datetime(2026, 9, 28, 4, 0, tzinfo=UTC)),
        sleep=time.sleep,
    )
    client.set_handler(handler)
    client.subscribe(frozenset({"NSE:OPT"}))
    client.start()
    time.sleep(0.1)
    assert callbacks
    callbacks[0](
        {
            "symbol": "NSE:OPT",
            "ltp": "101.25",
            "bid": "101.00",
            "ask": "101.50",
        }
    )
    time.sleep(0.2)
    assert received
    assert received[0][0] == "NSE:OPT"
    assert received[0][2] is QuoteMonitorSource.WEBSOCKET
    client.stop()
    hub.stop()


def test_protection_falls_back_to_rest_when_hub_down(
    tmp_path: Path,
    settings: FyersSettings,
) -> None:
    clock = FrozenClock(datetime(2026, 9, 28, 4, 0, tzinfo=UTC))
    ws_monitor = build_quote_monitor(
        config=ProtectionConfig(ws_enabled=True, quote_source="shared_hub"),
        repo_root=tmp_path,
        settings=settings,
        clock=clock,
    )
    assert ws_monitor is not None
    assert ws_monitor.ws_connected is False
    received: list[str] = []
    rest = RestQuoteMonitor(
        clock,
        lambda symbols: {
            symbol: MarketQuote(
                last=Price.snap("100.00", TickSize.of(Decimal("0.05"))),
                bid=None,
                ask=None,
            )
            for symbol in symbols
        },
    )

    def handler(
        symbol: str,
        _quote: MarketQuote,
        source: QuoteMonitorSource,
        _received_at: datetime,
    ) -> None:
        received.append(f"{symbol}:{source.value}")

    rest.set_handler(handler)
    rest.subscribe(frozenset({"NSE:OPT"}))
    rest.start()
    rest.tick()
    assert received == ["NSE:OPT:REST"]


def test_direct_ws_disallowed_when_tick_daemon_lock_held(tmp_path: Path) -> None:
    lock = DataSocketGuard.acquire(tmp_path, "fno-data-tick")
    with pytest.raises(ValueError, match="direct_ws disallowed"):
        build_quote_monitor(
            config=ProtectionConfig(ws_enabled=True, quote_source="direct_ws"),
            repo_root=tmp_path,
            settings=_settings_stub(),
            clock=FrozenClock(datetime(2026, 9, 28, tzinfo=UTC)),
        )
    DataSocketGuard.release(lock)


def test_process_level_guard_blocks_second_socket_in_same_process(
    tmp_path: Path,
    settings: FyersSettings,
) -> None:
    hub = SharedDataHubServer(
        settings,
        tmp_path,
        clock=FrozenClock(datetime(2026, 9, 28, 4, 0, tzinfo=UTC)),
        socket_factory=lambda **_kwargs: _FakeSocket(),
        sleep=time.sleep,
    )
    hub.start()
    assert process_has_data_socket()
    with pytest.raises(DataSocketAlreadyOpenError):
        DataSocketGuard.acquire(tmp_path, "second-attempt")
    hub.stop()
