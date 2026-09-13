"""Fyers websocket tick stream adapter."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from trading.data.events import CanonicalMarketEvent
from trading.data.normalize import normalize_fyers_ws_tick
from trading.data.ports import EventStore
from trading.data.quality import assess_tick
from trading.data.settings import FyersSettings
from trading.domain.clock import Clock
from trading.domain.enums import DataQuality

__all__ = ["FyersTickStream", "TickStreamResult"]

_DEBUG_LOG = Path(
    "/Users/apple/Documents/manasjit/fno-automated/.cursor/debug-b45cd5.log"
)


def _agent_log(
    location: str,
    message: str,
    data: dict[str, object],
    hypothesis_id: str,
    *,
    run_id: str = "pre-fix",
) -> None:
    try:
        payload = {
            "sessionId": "b45cd5",
            "timestamp": int(time.time() * 1000),
            "location": location,
            "message": message,
            "data": data,
            "hypothesisId": hypothesis_id,
            "runId": run_id,
        }
        with _DEBUG_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
    except OSError:
        pass


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


@dataclass(frozen=True, slots=True)
class TickStreamResult:
    """Outcome of a websocket tick collection session."""

    symbol: str
    ticks: tuple[CanonicalMarketEvent, ...]
    stopped_reason: str
    reconnects: int = 0


class FyersTickStream:
    """Subscribe to Fyers websocket ticks and persist canonical TICK events."""

    def __init__(
        self,
        settings: FyersSettings,
        clock: Clock,
        store: EventStore,
        *,
        repo_root: Path,
        normalization_version: str,
        channel: int = 11,
        socket_factory: Callable[..., _WsSocket] | None = None,
        reconnect: bool = False,
        reconnect_attempts: int = 1,
        reconnect_backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._store = store
        self._repo_root = repo_root
        self._normalization_version = normalization_version
        self._channel = channel
        self._socket_factory = socket_factory or self._default_socket_factory
        self._reconnect = reconnect
        self._reconnect_attempts = reconnect_attempts
        self._reconnect_backoff_seconds = reconnect_backoff_seconds
        self._sleep = sleep

    def collect(
        self,
        symbol: str,
        *,
        max_ticks: int,
        duration_seconds: int,
        tick_max_age_ms: int = 5_000,
        stop: threading.Event | None = None,
    ) -> TickStreamResult:
        """Connect, subscribe and collect ticks until limit, duration or stop."""
        if max_ticks < 1:
            raise ValueError("max_ticks must be at least 1")
        ticks: list[CanonicalMarketEvent] = []
        halt = stop or threading.Event()
        deadline = (
            None if duration_seconds <= 0 else time.monotonic() + duration_seconds
        )
        reconnects = 0

        def on_message(message: dict[str, Any]) -> None:
            if halt.is_set():
                return
            if message.get("symbol") not in {symbol, None} and "ltp" not in message:
                return
            receive_time = self._clock.now_utc()
            event = normalize_fyers_ws_tick(
                message,
                symbol=symbol,
                normalization_version=self._normalization_version,
                receive_time=receive_time,
            )
            quality = assess_tick(event, now=receive_time, max_age_ms=tick_max_age_ms)
            if quality.state is DataQuality.INVALID:
                return
            self._store.append_canonical(event)
            ticks.append(event)
            if len(ticks) >= max_ticks:
                halt.set()

        attempts = 0
        stopped_reason = "duration"
        log_dir = self._repo_root / "data" / "logs"
        while not halt.is_set():
            # #region agent log
            _agent_log(
                "ws.py:collect",
                "before_socket_factory",
                {"log_dir": str(log_dir), "log_dir_exists": log_dir.exists()},
                "H1",
            )
            # #endregion
            log_dir.mkdir(parents=True, exist_ok=True)
            # #region agent log
            _agent_log(
                "ws.py:collect",
                "after_mkdir",
                {"log_dir_exists": log_dir.is_dir()},
                "H2",
            )
            # #endregion
            try:
                socket = self._socket_factory(
                    access_token=self._settings.auth_header,
                    write_to_file=False,
                    log_path=str(log_dir),
                    reconnect=False,
                    on_message=on_message,
                )
            except OSError as exc:
                # #region agent log
                _agent_log(
                    "ws.py:collect",
                    "socket_factory_failed",
                    {"error_type": type(exc).__name__, "error": str(exc)},
                    "H1",
                )
                # #endregion
                raise
            try:
                socket.connect()
                socket.subscribe(
                    symbols=[symbol],
                    data_type="SymbolUpdate",
                    channel=self._channel,
                )
                socket.keep_running()
                while not halt.is_set():
                    if deadline is not None and time.monotonic() >= deadline:
                        stopped_reason = "duration"
                        halt.set()
                        break
                    if len(ticks) >= max_ticks:
                        stopped_reason = "max_ticks"
                        halt.set()
                        break
                    self._sleep(0.1)
            except (OSError, RuntimeError):
                attempts += 1
                if not self._reconnect or attempts >= self._reconnect_attempts:
                    stopped_reason = "error"
                    break
                reconnects += 1
                self._sleep(self._reconnect_backoff_seconds * attempts)
                continue
            finally:
                socket.close_connection()
            if halt.is_set() or deadline is not None:
                break
            if not self._reconnect:
                break
            attempts += 1
            if attempts >= self._reconnect_attempts:
                break
            reconnects += 1
            self._sleep(self._reconnect_backoff_seconds * attempts)
        if len(ticks) >= max_ticks:
            stopped_reason = "max_ticks"
        if stop is not None and stop.is_set() and stopped_reason == "duration":
            stopped_reason = "signal"
        return TickStreamResult(
            symbol=symbol,
            ticks=tuple(ticks),
            stopped_reason=stopped_reason,
            reconnects=reconnects,
        )

    def run_daemon(
        self,
        symbol: str,
        *,
        tick_max_age_ms: int = 5_000,
        stop: threading.Event | None = None,
    ) -> TickStreamResult:
        """Run until SIGTERM/stop. max_ticks is effectively unbounded."""
        return self.collect(
            symbol,
            max_ticks=1_000_000_000,
            duration_seconds=0,
            tick_max_age_ms=tick_max_age_ms,
            stop=stop,
        )

    @staticmethod
    def _default_socket_factory(**kwargs: Any) -> _WsSocket:
        from fyers_apiv3.FyersWebsocket import data_ws

        return cast(_WsSocket, data_ws.FyersDataSocket(**kwargs))
