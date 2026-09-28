"""Fyers websocket quote monitor for open PAPER positions."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from trading.data.settings import FyersSettings
from trading.domain.clock import Clock
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import QuoteMonitorSource
from trading.domain.primitives import Price, TickSize
from trading.runtime.quote_monitor import QuoteHandler

__all__ = ["FyersWsQuoteMonitor"]


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


class FyersWsQuoteMonitor:
    """Background websocket subscriber for protection quotes."""

    def __init__(
        self,
        settings: FyersSettings,
        clock: Clock,
        repo_root: Path,
        *,
        tick_handler: Callable[[str, dict[str, object], datetime], MarketQuote | None]
        | None = None,
        sleeper: Callable[[float], None] | None = None,
        socket_factory: Callable[..., _WsSocket] | None = None,
        reconnect_backoff_seconds: float = 1.0,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._repo_root = repo_root
        self._tick_handler = tick_handler or _default_tick_quote
        self._sleeper = sleeper
        self._socket_factory = socket_factory or self._default_socket_factory
        self._reconnect_backoff_seconds = reconnect_backoff_seconds
        self._symbols: frozenset[str] = frozenset()
        self._handler: QuoteHandler | None = None
        self._running = False
        self._connected = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def set_handler(self, handler: QuoteHandler) -> None:
        self._handler = handler

    def subscribe(self, symbols: frozenset[str]) -> None:
        self._symbols = symbols

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        with self._lock:
            self._connected = False

    @property
    def ws_connected(self) -> bool:
        with self._lock:
            return self._connected

    def _set_connected(self, connected: bool) -> None:
        with self._lock:
            self._connected = connected

    def _run(self) -> None:
        sleeper = self._sleeper or time.sleep
        log_dir = self._repo_root / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        reconnect_attempt = 0
        while self._running and not self._stop.is_set():
            if not self._symbols:
                self._set_connected(False)
                sleeper(0.5)
                continue
            subscribed = tuple(sorted(self._symbols))
            self._set_connected(False)
            received_tick = False

            def on_message(
                message: dict[str, object],
                *,
                _subscribed: tuple[str, ...] = subscribed,
            ) -> None:
                nonlocal received_tick
                if self._handle_ws_message(message, _subscribed):
                    received_tick = True

            socket = self._socket_factory(
                access_token=self._settings.auth_header,
                write_to_file=False,
                log_path=str(log_dir),
                reconnect=False,
                on_message=on_message,
            )
            try:
                socket.connect()
                socket.subscribe(
                    symbols=list(subscribed),
                    data_type="SymbolUpdate",
                    channel=11,
                )
                socket.keep_running()
                while not self._stop.is_set():
                    if received_tick:
                        self._set_connected(True)
                    sleeper(0.1)
                reconnect_attempt = 0
            except (OSError, RuntimeError):
                self._set_connected(False)
                reconnect_attempt += 1
            finally:
                socket.close_connection()
                if not received_tick:
                    self._set_connected(False)
            if self._stop.is_set() or not self._running:
                break
            backoff = self._reconnect_backoff_seconds * max(reconnect_attempt, 1)
            sleeper(backoff)

    def _handle_ws_message(
        self, message: dict[str, object], subscribed: tuple[str, ...]
    ) -> bool:
        if self._stop.is_set():
            return False
        symbol = _resolve_symbol(message, subscribed)
        if symbol is None or symbol not in self._symbols:
            return False
        received_at = self._clock.now_utc()
        quote = self._tick_handler(symbol, message, received_at)
        if quote is None:
            return False
        self._set_connected(True)
        handler = self._handler
        if handler is not None:
            handler(symbol, quote, QuoteMonitorSource.WEBSOCKET, received_at)
        return True

    @staticmethod
    def _default_socket_factory(**kwargs: Any) -> _WsSocket:
        from fyers_apiv3.FyersWebsocket import data_ws  # noqa: PLC0415

        return cast(_WsSocket, data_ws.FyersDataSocket(**kwargs))


def _resolve_symbol(
    message: dict[str, object], subscribed: Sequence[str]
) -> str | None:
    raw = message.get("symbol")
    if raw is not None:
        return str(raw)
    if len(subscribed) == 1:
        return subscribed[0]
    return None


def _default_tick_quote(
    _symbol: str, message: dict[str, object], _received_at: datetime
) -> MarketQuote | None:
    last = message.get("ltp", message.get("lp"))
    bid = message.get("bid_price", message.get("bid"))
    ask = message.get("ask_price", message.get("ask"))
    if last is None and bid is None and ask is None:
        return None
    tick = TickSize.of(Decimal("0.05"))
    anchor = last if last is not None else bid
    if anchor is None:
        return None
    last_price = Price.snap(str(anchor), tick)
    bid_price = Price.snap(str(bid), tick) if bid is not None else None
    ask_price = Price.snap(str(ask), tick) if ask is not None else None
    return MarketQuote(
        last=last_price,
        bid=bid_price,
        ask=ask_price,
    )
