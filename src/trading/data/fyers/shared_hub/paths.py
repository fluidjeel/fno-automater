"""Filesystem paths for the local shared Fyers data socket hub."""

from __future__ import annotations

from pathlib import Path

__all__ = [
    "control_socket_path",
    "hub_root",
    "status_path",
    "stream_socket_path",
]


def hub_root(repo_root: Path) -> Path:
    """Return the hub directory under the repo data tree."""
    return repo_root / "data" / "fyers" / "shared_hub"


def control_socket_path(repo_root: Path) -> Path:
    """Unix socket for subscribe/unsubscribe control requests."""
    return hub_root(repo_root) / "control.sock"


def stream_socket_path(repo_root: Path) -> Path:
    """Unix socket for low-latency quote and depth fan-out."""
    return hub_root(repo_root) / "stream.sock"


def status_path(repo_root: Path) -> Path:
    """JSON heartbeat for hub ownership and subscriber counts."""
    return hub_root(repo_root) / "status.json"
