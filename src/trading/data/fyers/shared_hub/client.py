"""Clients for the shared Fyers data socket hub."""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trading.data.fyers.shared_hub.paths import (
    control_socket_path,
    stream_socket_path,
)
from trading.data.fyers.shared_hub.protocol import (
    StreamMessage,
    decode_line,
    encode_line,
)
from trading.data.fyers.shared_hub.status import load_shared_hub_status
from trading.domain.clock import Clock, WallClock
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import QuoteMonitorSource
from trading.domain.primitives import Price, TickSize

__all__ = ["SharedDataSocketClient", "SharedHubClient"]

QuoteHandler = Callable[[str, MarketQuote, QuoteMonitorSource, datetime], None]
DepthHandler = Callable[[str, dict[str, object], datetime], None]


class SharedHubClient:
    """Low-level shared hub client for control and stream subscriptions."""

    def __init__(
        self,
        repo_root: Path,
        *,
        owner: str,
        data_type: str = "SymbolUpdate",
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
        connect_timeout_seconds: float = 0.25,
    ) -> None:
        self._repo_root = repo_root
        self._clock = clock or WallClock()
        self._owner = owner
        self._data_type = data_type
        self._sleep = sleep
        self._connect_timeout_seconds = connect_timeout_seconds
        self._symbols: frozenset[str] = frozenset()
        self._depth_handler: DepthHandler | None = None
        self._quote_handler: QuoteHandler | None = None
        self._running = False
        self._connected = False
        self._last_message_at: datetime | None = None
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None

    def set_quote_handler(self, handler: QuoteHandler | None) -> None:
        self._quote_handler = handler

    def set_depth_handler(self, handler: DepthHandler | None) -> None:
        self._depth_handler = handler

    def subscribe(self, symbols: frozenset[str]) -> None:
        self._symbols = symbols
        if self._running:
            self._sync_subscriptions()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._halt.clear()
        self._sync_subscriptions()
        self._thread = threading.Thread(target=self._run_stream, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._halt.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._connected = False

    @property
    def hub_connected(self) -> bool:
        if not self._connected:
            return False
        if self._last_message_at is None:
            status = load_shared_hub_status(self._repo_root)
            return status is not None and status.hub_active
        return self._clock.now_utc() - self._last_message_at <= timedelta(seconds=5)

    def _sync_subscriptions(self) -> None:
        if not self._symbols:
            payload = {
                "op": "replace",
                "owner": self._owner,
                "symbols": [],
                "data_type": self._data_type,
            }
        else:
            payload = {
                "op": "replace",
                "owner": self._owner,
                "symbols": sorted(self._symbols),
                "data_type": self._data_type,
            }
        self._send_control(dict(payload))

    def _send_control(self, payload: dict[str, object]) -> bool:
        path = control_socket_path(self._repo_root)
        if not path.exists():
            return False
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(self._connect_timeout_seconds)
        try:
            client.connect(str(path))
            client.sendall(encode_line(payload))
            response = client.recv(4096)
            if not response:
                return False
            line = response.split(b"\n", 1)[0]
            body = decode_line(line)
            return bool(body.get("ok"))
        except OSError:
            return False
        finally:
            client.close()

    def _run_stream(self) -> None:
        while self._running and not self._halt.is_set():
            path = stream_socket_path(self._repo_root)
            if not path.exists():
                self._connected = False
                self._sleep(0.5)
                continue
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(1.0)
            try:
                client.connect(str(path))
                self._connected = True
                buffer = b""
                while not self._halt.is_set():
                    try:
                        chunk = client.recv(4096)
                    except TimeoutError:
                        continue
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if not line:
                            continue
                        self._dispatch(StreamMessage.from_payload(decode_line(line)))
            except OSError:
                self._connected = False
            finally:
                client.close()
            self._sleep(0.2)

    def _dispatch(self, message: StreamMessage) -> None:
        if message.data_type != self._data_type:
            return
        if self._symbols and message.symbol not in self._symbols:
            return
        self._last_message_at = message.received_at
        if message.data_type == "DepthUpdate" and self._depth_handler is not None:
            self._depth_handler(message.symbol, message.payload, message.received_at)
            return
        if self._quote_handler is None:
            return
        quote = _quote_from_symbol_update(message.payload)
        if quote is None:
            return
        self._quote_handler(
            message.symbol,
            quote,
            QuoteMonitorSource.WEBSOCKET,
            message.received_at,
        )


class SharedDataSocketClient:
    """Quote monitor that consumes SymbolUpdate data from the shared hub."""

    def __init__(
        self,
        repo_root: Path,
        *,
        owner: str = "paper-protection",
        clock: Clock | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = SharedHubClient(
            repo_root,
            owner=owner,
            data_type="SymbolUpdate",
            clock=clock,
            sleep=sleep,
        )
        self._handler: QuoteHandler | None = None

    def set_handler(self, handler: QuoteHandler) -> None:
        self._handler = handler
        self._client.set_quote_handler(handler)

    def subscribe(self, symbols: frozenset[str]) -> None:
        self._client.subscribe(symbols)

    def start(self) -> None:
        self._client.start()

    def stop(self) -> None:
        self._client.stop()

    @property
    def ws_connected(self) -> bool:
        return self._client.hub_connected


def _quote_from_symbol_update(message: dict[str, object]) -> MarketQuote | None:
    last = message.get("ltp", message.get("lp"))
    bid = message.get("bid")
    ask = message.get("ask")
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
