"""Shared Fyers data_ws hub owned by the tick daemon."""

from __future__ import annotations

import contextlib
import logging
import socket
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from trading.data.fyers.data_socket_guard import (
    DataSocketGuard,
    DataSocketLock,
)
from trading.data.fyers.shared_hub.paths import (
    control_socket_path,
    hub_root,
    stream_socket_path,
)
from trading.data.fyers.shared_hub.protocol import (
    ControlRequest,
    ControlResponse,
    StreamMessage,
    decode_line,
    encode_line,
)
from trading.data.fyers.shared_hub.status import (
    SharedHubStatus,
    write_shared_hub_status,
)
from trading.data.fyers.shared_hub.subscriptions import (
    SubscriptionKey,
    SubscriptionRegistry,
)
from trading.data.settings import FyersSettings
from trading.domain.clock import Clock, WallClock

__all__ = ["SharedDataHubServer"]

logger = logging.getLogger(__name__)


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


class SharedDataHubServer:
    """Own the account data_ws socket and fan out updates over local Unix sockets."""

    def __init__(
        self,
        settings: FyersSettings,
        repo_root: Path,
        *,
        owner: str = "fno-data-tick",
        channel: int = 11,
        clock: Clock | None = None,
        socket_factory: Callable[..., _WsSocket] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._repo_root = repo_root
        self._clock = clock or WallClock()
        self._owner = owner
        self._channel = channel
        self._socket_factory = socket_factory or self._default_socket_factory
        self._sleep = sleep
        self._registry = SubscriptionRegistry()
        self._lock: DataSocketLock | None = None
        self._socket: _WsSocket | None = None
        self._halt = threading.Event()
        self._stream_clients: list[socket.socket] = []
        self._stream_lock = threading.Lock()
        self._control_server: socket.socket | None = None
        self._stream_server: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._tick_handler: Callable[[str, dict[str, Any], datetime], None] | None = (
            None
        )

    def set_tick_handler(
        self,
        handler: Callable[[str, dict[str, Any], datetime], None] | None,
    ) -> None:
        """Optional callback for tick-daemon persistence on SymbolUpdate."""
        self._tick_handler = handler

    def start(
        self,
        initial_symbols: Sequence[tuple[str, str]] = (),
    ) -> None:
        """Acquire the data socket lock, open the hub, and begin serving."""
        self._lock = DataSocketGuard.acquire(
            self._repo_root,
            self._owner,
            clock=self._clock,
        )
        hub_root(self._repo_root).mkdir(parents=True, exist_ok=True)
        self._start_socket_servers()
        for symbol, data_type in initial_symbols:
            self._registry.subscribe(self._owner, symbol, data_type)
        self._open_fyers_socket()
        self._sync_fyers_subscriptions()
        socket_thread = threading.Thread(target=self._run_socket, daemon=True)
        socket_thread.start()
        self._threads.append(socket_thread)
        self._write_status()
        status_thread = threading.Thread(target=self._status_loop, daemon=True)
        status_thread.start()
        self._threads.append(status_thread)

    def stop(self) -> None:
        """Shut down sockets, clients, and release the host lock."""
        self._halt.set()
        if self._socket is not None:
            with contextlib.suppress(OSError, RuntimeError):
                self._socket.close_connection()
            self._socket = None
        for server in (self._control_server, self._stream_server):
            if server is not None:
                with contextlib.suppress(OSError):
                    server.close()
        with self._stream_lock:
            for client in self._stream_clients:
                with contextlib.suppress(OSError):
                    client.close()
            self._stream_clients.clear()
        for path in (
            control_socket_path(self._repo_root),
            stream_socket_path(self._repo_root),
        ):
            path.unlink(missing_ok=True)
        if self._lock is not None:
            DataSocketGuard.release(self._lock)
            self._lock = None

    def run_until_stopped(self) -> None:
        """Block until stop() is called."""
        while not self._halt.is_set():
            self._sleep(0.2)

    def _run_socket(self) -> None:
        if self._socket is None:
            return
        try:
            self._socket.keep_running()
        except (OSError, RuntimeError):
            logger.exception("shared data socket stopped with error")
        finally:
            self._halt.set()

    def registry(self) -> SubscriptionRegistry:
        """Expose the subscription registry for tests."""
        return self._registry

    def _open_fyers_socket(self) -> None:
        def on_message(message: dict[str, Any]) -> None:
            if self._halt.is_set():
                return
            if message.get("s") == "error":
                if message.get("type") in {"cn", "AUTH"}:
                    self._halt.set()
                return
            symbol = message.get("symbol")
            if not isinstance(symbol, str):
                return
            data_type = self._infer_data_type(message)
            received_at = self._clock.now_utc()
            if data_type == "SymbolUpdate" and self._tick_handler is not None:
                self._tick_handler(symbol, message, received_at)
            self._broadcast(
                StreamMessage(
                    symbol=symbol,
                    data_type=data_type,
                    payload=dict(message),
                    received_at=received_at,
                )
            )

        log_dir = self._repo_root / "data" / "logs" / "shared_hub"
        log_dir.mkdir(parents=True, exist_ok=True)
        self._socket = self._socket_factory(
            access_token=self._settings.auth_header,
            write_to_file=False,
            log_path=str(log_dir),
            reconnect=True,
            on_message=on_message,
        )
        self._socket.connect()

    def _start_socket_servers(self) -> None:
        control_path = control_socket_path(self._repo_root)
        stream_path = stream_socket_path(self._repo_root)
        control_path.unlink(missing_ok=True)
        stream_path.unlink(missing_ok=True)
        self._control_server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._control_server.bind(str(control_path))
        self._control_server.listen(8)
        self._stream_server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._stream_server.bind(str(stream_path))
        self._stream_server.listen(8)
        control_thread = threading.Thread(target=self._serve_control, daemon=True)
        stream_thread = threading.Thread(target=self._serve_stream, daemon=True)
        control_thread.start()
        stream_thread.start()
        self._threads.extend([control_thread, stream_thread])

    def _serve_control(self) -> None:
        server = self._control_server
        if server is None:
            return
        server.settimeout(0.5)
        while not self._halt.is_set():
            try:
                client, _addr = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_control_client,
                args=(client,),
                daemon=True,
            ).start()

    def _handle_control_client(self, client: socket.socket) -> None:
        buffer = b""
        try:
            while not self._halt.is_set():
                chunk = client.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line:
                        continue
                    response = self._handle_control_request(decode_line(line))
                    client.sendall(encode_line(response.to_payload()))
        except OSError:
            pass
        finally:
            client.close()
            self._write_status()

    def _handle_control_request(self, payload: Mapping[str, Any]) -> ControlResponse:
        request = ControlRequest.from_payload(dict(payload))
        if request.op == "ping":
            return ControlResponse(ok=True)
        if request.op == "replace":
            return self._replace_owner(payload)
        if request.op in {"subscribe", "unsubscribe"}:
            return self._mutate_subscription(request)
        return ControlResponse(ok=False, error=f"unknown op {request.op}")

    def _replace_owner(self, payload: Mapping[str, Any]) -> ControlResponse:
        owner = str(payload.get("owner", ""))
        data_type = str(payload.get("data_type", "SymbolUpdate"))
        symbols = payload.get("symbols", [])
        if not isinstance(symbols, list):
            return ControlResponse(ok=False, error="replace requires symbols list")
        desired = {
            SubscriptionKey(symbol=str(symbol), data_type=data_type)
            for symbol in symbols
        }
        activated, deactivated = self._registry.replace_owner(owner, desired)
        self._apply_subscription_delta(activated, deactivated)
        self._write_status()
        return ControlResponse(ok=True)

    def _mutate_subscription(self, request: ControlRequest) -> ControlResponse:
        if request.symbol is None or request.data_type is None:
            return ControlResponse(
                ok=False, error="subscribe requires symbol and data_type"
            )
        if request.op == "subscribe":
            delta = self._registry.subscribe(
                request.owner,
                request.symbol,
                request.data_type,
            )
            self._apply_subscription_delta(delta, set())
        else:
            delta = self._registry.unsubscribe(
                request.owner,
                request.symbol,
                request.data_type,
            )
            self._apply_subscription_delta(set(), delta)
        self._write_status()
        return ControlResponse(ok=True)

    def _apply_subscription_delta(
        self,
        activated: set[SubscriptionKey],
        deactivated: set[SubscriptionKey],
    ) -> None:
        if activated or deactivated:
            self._sync_fyers_subscriptions()

    def _sync_fyers_subscriptions(self) -> None:
        if self._socket is None:
            return
        for data_type in ("SymbolUpdate", "DepthUpdate"):
            symbols = self._registry.keys_for_data_type(data_type)
            if not symbols:
                continue
            self._socket.subscribe(
                symbols=list(symbols),
                data_type=data_type,
                channel=self._channel,
            )

    def _serve_stream(self) -> None:
        server = self._stream_server
        if server is None:
            return
        server.settimeout(0.5)
        while not self._halt.is_set():
            try:
                client, _addr = server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            with self._stream_lock:
                self._stream_clients.append(client)
            self._write_status()

    def _broadcast(self, message: StreamMessage) -> None:
        payload = encode_line(message.to_payload())
        dead: list[socket.socket] = []
        with self._stream_lock:
            for client in self._stream_clients:
                try:
                    client.sendall(payload)
                except OSError:
                    dead.append(client)
            for client in dead:
                self._stream_clients.remove(client)

    def _status_loop(self) -> None:
        while not self._halt.is_set():
            self._write_status()
            self._sleep(1.0)

    def _write_status(self) -> None:
        active = self._registry.active_keys()
        write_shared_hub_status(
            self._repo_root,
            SharedHubStatus(
                as_of=self._clock.now_utc(),
                data_socket_owner=self._owner,
                data_socket_count=1 if self._socket is not None else 0,
                hub_active=self._socket is not None and not self._halt.is_set(),
                stream_subscribers=len(self._stream_clients),
                control_connections=0,
                subscriber_counts=self._registry.subscriber_counts(),
                active_symbol_updates=sum(
                    1 for key in active if key.data_type == "SymbolUpdate"
                ),
                active_depth_updates=sum(
                    1 for key in active if key.data_type == "DepthUpdate"
                ),
            ),
        )

    @staticmethod
    def _infer_data_type(message: Mapping[str, Any]) -> str:
        if message.get("type") == "dp" or message.get("bid_price1") is not None:
            return "DepthUpdate"
        return "SymbolUpdate"

    @staticmethod
    def _default_socket_factory(**kwargs: Any) -> _WsSocket:
        from fyers_apiv3.FyersWebsocket import data_ws

        return cast(_WsSocket, data_ws.FyersDataSocket(**kwargs))
