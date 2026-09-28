"""Process and host guard ensuring at most one Fyers data_ws socket."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from trading.domain.clock import Clock, WallClock

__all__ = [
    "DataSocketAlreadyOpenError",
    "DataSocketGuard",
    "DataSocketLock",
    "data_socket_lock_path",
    "is_data_socket_locked",
    "process_has_data_socket",
    "set_process_data_socket_open",
]

logger = logging.getLogger(__name__)

_PROCESS_SOCKET_OPEN = False


class DataSocketAlreadyOpenError(RuntimeError):
    """Raised when a second data_ws socket open is attempted on this host."""


@dataclass(frozen=True, slots=True)
class DataSocketLock:
    """Exclusive host lock for the account data_ws socket."""

    path: Path
    owner: str
    pid: int

    def release(self) -> None:
        """Drop the lock file when the owning process closes the socket."""
        if self.path.exists():
            try:
                payload = self.path.read_text(encoding="utf-8")
            except OSError:
                payload = ""
            if f"pid={self.pid}" in payload:
                self.path.unlink(missing_ok=True)


def data_socket_lock_path(repo_root: Path) -> Path:
    """Return the canonical lock file path under the repo data directory."""
    return repo_root / "data" / "fyers" / "data_ws.lock"


def is_data_socket_locked(repo_root: Path) -> bool:
    """Return whether an active data_ws lock is held on this host."""
    path = data_socket_lock_path(repo_root)
    if not path.exists():
        return False
    payload = path.read_text(encoding="utf-8")
    for line in payload.splitlines():
        if line.startswith("pid="):
            try:
                pid = int(line.split("=", 1)[1])
            except ValueError:
                return True
            if pid == os.getpid():
                return True
            try:
                os.kill(pid, 0)
            except OSError:
                path.unlink(missing_ok=True)
                return False
            return True
    return True


def process_has_data_socket() -> bool:
    """Return whether this process already opened a guarded data_ws socket."""
    return _PROCESS_SOCKET_OPEN


def set_process_data_socket_open(opened: bool) -> None:
    """Record process-level data_ws ownership for fail-fast guarding."""
    global _PROCESS_SOCKET_OPEN  # noqa: PLW0603
    _PROCESS_SOCKET_OPEN = opened


class DataSocketGuard:
    """Acquire and release the single-host data_ws lock."""

    @staticmethod
    def acquire(
        repo_root: Path,
        owner: str,
        *,
        clock: Clock | None = None,
    ) -> DataSocketLock:
        """Acquire the lock or refuse when another live owner holds it."""
        active_clock = clock or WallClock()
        if process_has_data_socket():
            msg = "process already owns a Fyers data_ws socket"
            logger.error(msg)
            raise DataSocketAlreadyOpenError(msg)
        path = data_socket_lock_path(repo_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        if is_data_socket_locked(repo_root):
            existing = path.read_text(encoding="utf-8").strip()
            msg = f"refusing second Fyers data_ws socket; lock held: {existing or path}"
            logger.error(msg)
            raise DataSocketAlreadyOpenError(msg)
        now = active_clock.now_utc().isoformat()
        payload = f"owner={owner}\npid={os.getpid()}\ncreated_at={now}\n"
        path.write_text(payload, encoding="utf-8")
        set_process_data_socket_open(True)
        return DataSocketLock(path=path, owner=owner, pid=os.getpid())

    @staticmethod
    def release(lock: DataSocketLock) -> None:
        """Release a previously acquired lock and clear process ownership."""
        lock.release()
        set_process_data_socket_open(False)
