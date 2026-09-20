"""Dependency-free local HTTP server and optional Oracle SSH snapshot bridge."""

from __future__ import annotations

import json
import mimetypes
import shlex
import subprocess
import threading
import time
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from trading.dashboard.collector import build_dashboard_snapshot

_STATIC = Path(__file__).with_name("static")


def _remote_path(path: str) -> str:
    """Quote a remote path while preserving a leading home-directory marker."""
    if path == "~":
        return '"$HOME"'
    if path.startswith("~/"):
        return f'"$HOME"/{shlex.quote(path[2:])}'
    return shlex.quote(path)


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    repo_root: Path
    bind_host: str = "127.0.0.1"
    port: int = 8765
    oracle_host: str = ""
    ssh_key: Path | None = None
    oracle_root: str = "~/fno-automated"
    refresh_seconds: int = 5
    open_browser: bool = True


class SnapshotProvider:
    def __init__(self, config: DashboardConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._last_fetch = 0.0
        self._cached: dict[str, Any] | None = None

    def get(self) -> dict[str, Any]:
        with self._lock:
            if (
                self._cached is not None
                and time.monotonic() - self._last_fetch < self._config.refresh_seconds
            ):
                return self._cached
            try:
                snapshot = (
                    self._remote()
                    if self._config.oracle_host
                    else build_dashboard_snapshot(self._config.repo_root)
                )
                snapshot["connection"] = {
                    "status": "CONNECTED",
                    "target": self._config.oracle_host or "LOCAL",
                }
                self._cached = snapshot
            except (
                OSError,
                ValueError,
                subprocess.SubprocessError,
                json.JSONDecodeError,
            ) as exc:
                if self._cached is None:
                    self._cached = build_dashboard_snapshot(self._config.repo_root)
                self._cached["connection"] = {
                    "status": "DEGRADED",
                    "target": self._config.oracle_host or "LOCAL",
                    "error": str(exc)[:240],
                }
            self._last_fetch = time.monotonic()
            return self._cached

    def _remote(self) -> dict[str, Any]:
        if self._config.oracle_host.startswith("-"):
            raise ValueError("Oracle SSH target cannot begin with '-'")
        base = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4"]
        if self._config.ssh_key is not None:
            base.extend(["-i", str(self._config.ssh_key)])
        installed_probe = (
            f"cd {_remote_path(self._config.oracle_root)} && "
            'TRADING_UV="$HOME/.local/bin/uv"; '
            '[ -x "$TRADING_UV" ] || TRADING_UV=uv; '
            '"$TRADING_UV" run trading dashboard snapshot --source oracle'
        )
        command = [*base, self._config.oracle_host, installed_probe]
        result = subprocess.run(  # noqa: S603 - argv only; remote root is quoted
            command, check=False, capture_output=True, text=True, timeout=12
        )
        if result.returncode == 0:
            loaded = json.loads(result.stdout)
            if not isinstance(loaded, dict):
                raise ValueError("Oracle probe returned an invalid snapshot")
            return loaded

        # Keep the terminal usable before the dashboard package is deployed.
        # The probe executes from stdin and leaves no source file on Oracle.
        collector_source = (
            Path(__file__).with_name("collector.py").read_text(encoding="utf-8")
        )
        probe_source = (
            collector_source
            + "\nprint(json.dumps("
            + "build_dashboard_snapshot(Path.cwd(), source='oracle')))\n"
        )
        ephemeral_probe = (
            f"cd {_remote_path(self._config.oracle_root)} && "
            'TRADING_UV="$HOME/.local/bin/uv"; '
            '[ -x "$TRADING_UV" ] || TRADING_UV=uv; '
            '"$TRADING_UV" run python -'
        )
        fallback = subprocess.run(  # noqa: S603 - local-owned source, no shell here
            [*base, self._config.oracle_host, ephemeral_probe],
            input=probe_source,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if fallback.returncode != 0:
            stderr = fallback.stderr.strip() or result.stderr.strip()
            detail = stderr.splitlines()[-1] if stderr else "SSH probe failed"
            raise OSError(detail)
        loaded = json.loads(fallback.stdout)
        if not isinstance(loaded, dict):
            raise ValueError("Oracle probe returned an invalid snapshot")
        return loaded


def _handler(provider: SnapshotProvider) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/api/snapshot":
                self._send_json(provider.get())
                return
            if path == "/api/health":
                self._send_json({"status": "ok"})
                return
            relative = "index.html" if path == "/" else path.lstrip("/")
            target = (_STATIC / relative).resolve()
            if _STATIC.resolve() not in target.parents or not target.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            body = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                mimetypes.guess_type(target.name)[0] or "application/octet-stream",
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, separators=(",", ":"), default=str).encode()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def serve_dashboard(config: DashboardConfig) -> None:
    """Run the local dashboard until interrupted."""
    server = ThreadingHTTPServer(
        (config.bind_host, config.port), _handler(SnapshotProvider(config))
    )
    url = f"http://{config.bind_host}:{server.server_port}"
    print(f"Oracle Desk Terminal: {url}")
    print(f"Evidence source: {config.oracle_host or 'local repository'}")
    if config.open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
