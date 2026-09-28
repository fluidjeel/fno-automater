"""Incremental Fyers data_ws DepthUpdate feed for promoted option strikes."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from trading.config.depth_promotion import DepthPromotionConfig
from trading.data.settings import FyersSettings
from trading.domain.clock import Clock

__all__ = [
    "DepthBookSnapshot",
    "IncrementalDepthSubscriptions",
    "PromotedDepthWebSocket",
    "SubscriptionAction",
]


@dataclass(frozen=True, slots=True)
class DepthBookSnapshot:
    """Latest top-of-book sizes from a depth update."""

    symbol: str
    bid_size: int
    ask_size: int
    received_at: datetime


@dataclass(frozen=True, slots=True)
class SubscriptionAction:
    """One incremental subscribe or unsubscribe delta."""

    kind: str
    symbols: tuple[str, ...]


@dataclass
class IncrementalDepthSubscriptions:
    """Track promoted-strike subscribe/unsubscribe deltas without reconnecting."""

    subscribed: set[str]
    actions: list[SubscriptionAction]

    def __init__(self) -> None:
        self.subscribed = set()
        self.actions = []

    def apply_delta(
        self,
        add: frozenset[str],
        remove: frozenset[str],
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return (added, removed) symbol tuples applied this call."""
        to_remove = tuple(
            sorted(symbol for symbol in remove if symbol in self.subscribed)
        )
        if to_remove:
            for symbol in to_remove:
                self.subscribed.discard(symbol)
            self.actions.append(SubscriptionAction("unsubscribe", to_remove))
        to_add = tuple(
            sorted(symbol for symbol in add if symbol not in self.subscribed)
        )
        if to_add:
            self.subscribed.update(to_add)
            self.actions.append(SubscriptionAction("subscribe", to_add))
        return to_add, to_remove


@runtime_checkable
class _DepthWsSocket(Protocol):
    def connect(self) -> None: ...

    def subscribe(
        self,
        symbols: list[str],
        data_type: str = "DepthUpdate",
        channel: int = 11,
    ) -> None: ...

    def unsubscribe(
        self,
        symbols: list[str],
        data_type: str = "DepthUpdate",
    ) -> None: ...

    def keep_running(self) -> None: ...

    def close_connection(self) -> None: ...


class PromotedDepthWebSocket:
    """Subscribe DepthUpdate channels only for promoted strikes."""

    def __init__(
        self,
        settings: FyersSettings,
        clock: Clock,
        repo_root: Path,
        config: DepthPromotionConfig,
        *,
        socket_factory: Callable[..., _DepthWsSocket] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._repo_root = repo_root
        self._config = config
        self._socket_factory = socket_factory or self._default_socket_factory
        self._sleeper = sleeper or time.sleep
        self._books: dict[str, DepthBookSnapshot] = {}
        self._subscriptions = IncrementalDepthSubscriptions()
        self._connected = False
        self._running = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._pending_add: set[str] = set()
        self._pending_remove: set[str] = set()
        self._socket: _DepthWsSocket | None = None

    @property
    def subscribed_symbols(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._subscriptions.subscribed)

    @property
    def ws_connected(self) -> bool:
        return self._connected

    @property
    def subscription_actions(self) -> tuple[SubscriptionAction, ...]:
        with self._lock:
            return tuple(self._subscriptions.actions)

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
        self._connected = False
        if self._socket is not None:
            self._socket.close_connection()
            self._socket = None

    def apply_delta(self, add: frozenset[str], remove: frozenset[str]) -> None:
        """Queue incremental subscription changes without reconnecting."""
        with self._lock:
            for symbol in remove:
                self._pending_remove.add(symbol)
                self._pending_add.discard(symbol)
            for symbol in add:
                if symbol not in self._subscriptions.subscribed:
                    self._pending_add.add(symbol)
                self._pending_remove.discard(symbol)

    def snapshot(self, symbol: str) -> DepthBookSnapshot | None:
        with self._lock:
            return self._books.get(symbol)

    def _run(self) -> None:
        log_dir = self._repo_root / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        while self._running and not self._stop.is_set():
            socket = self._socket_factory(
                access_token=self._settings.auth_header,
                write_to_file=False,
                log_path=str(log_dir),
                reconnect=False,
                on_message=self._on_message,
            )
            self._socket = socket
            try:
                socket.connect()
                self._connected = True
                with self._lock:
                    bootstrap_add = frozenset(
                        self._pending_add | self._subscriptions.subscribed
                    )
                    self._pending_add.clear()
                if bootstrap_add:
                    to_add, _ = self._subscriptions.apply_delta(
                        bootstrap_add, frozenset()
                    )
                    if to_add:
                        socket.subscribe(
                            list(to_add),
                            data_type="DepthUpdate",
                            channel=self._config.ws_channel,
                        )
                while not self._stop.is_set():
                    self._flush_pending(socket)
                    socket.keep_running()
                    self._sleeper(0.05)
            except (OSError, RuntimeError):
                self._connected = False
                self._sleeper(0.5)
            finally:
                if self._socket is not None:
                    self._socket.close_connection()
                    self._socket = None
                self._connected = False

    def _flush_pending(self, socket: _DepthWsSocket) -> None:
        with self._lock:
            pending_add = frozenset(self._pending_add)
            pending_remove = frozenset(self._pending_remove)
            self._pending_add.clear()
            self._pending_remove.clear()
        to_add, to_remove = self._subscriptions.apply_delta(
            pending_add,
            pending_remove,
        )
        if to_remove:
            socket.unsubscribe(list(to_remove), data_type="DepthUpdate")
            with self._lock:
                for symbol in to_remove:
                    self._books.pop(symbol, None)
        if to_add:
            socket.subscribe(
                list(to_add),
                data_type="DepthUpdate",
                channel=self._config.ws_channel,
            )

    def _on_message(self, message: dict[str, Any]) -> None:
        symbol = message.get("symbol")
        if not isinstance(symbol, str):
            return
        bid_size = _optional_int(message.get("bid_size1"))
        ask_size = _optional_int(message.get("ask_size1"))
        if bid_size is None or ask_size is None:
            return
        snapshot = DepthBookSnapshot(
            symbol=symbol,
            bid_size=bid_size,
            ask_size=ask_size,
            received_at=self._clock.now_utc(),
        )
        with self._lock:
            self._books[symbol] = snapshot

    @staticmethod
    def _default_socket_factory(**kwargs: Any) -> _DepthWsSocket:
        from fyers_apiv3.FyersWebsocket import data_ws  # noqa: PLC0415

        return cast(_DepthWsSocket, data_ws.FyersDataSocket(**kwargs))


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
