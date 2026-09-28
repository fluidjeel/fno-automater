"""Shared hub health and ownership status."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from trading.data.fyers.shared_hub.paths import status_path

__all__ = ["SharedHubStatus", "load_shared_hub_status", "write_shared_hub_status"]


@dataclass(frozen=True, slots=True)
class SharedHubStatus:
    """Heartbeat for the single-owner shared data socket hub."""

    as_of: datetime
    data_socket_owner: str
    data_socket_count: int
    hub_active: bool
    stream_subscribers: int
    control_connections: int
    subscriber_counts: dict[str, int]
    active_symbol_updates: int
    active_depth_updates: int

    def to_json(self) -> str:
        payload = {
            "as_of": self.as_of.isoformat(),
            "data_socket_owner": self.data_socket_owner,
            "data_socket_count": self.data_socket_count,
            "hub_active": self.hub_active,
            "stream_subscribers": self.stream_subscribers,
            "control_connections": self.control_connections,
            "subscriber_counts": self.subscriber_counts,
            "active_symbol_updates": self.active_symbol_updates,
            "active_depth_updates": self.active_depth_updates,
        }
        return json.dumps(payload, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> SharedHubStatus:
        payload = json.loads(raw)
        as_of = datetime.fromisoformat(str(payload["as_of"]))
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=UTC)
        return cls(
            as_of=as_of,
            data_socket_owner=str(payload["data_socket_owner"]),
            data_socket_count=int(payload["data_socket_count"]),
            hub_active=bool(payload["hub_active"]),
            stream_subscribers=int(payload["stream_subscribers"]),
            control_connections=int(payload["control_connections"]),
            subscriber_counts={
                str(owner): int(count)
                for owner, count in dict(payload.get("subscriber_counts", {})).items()
            },
            active_symbol_updates=int(payload.get("active_symbol_updates", 0)),
            active_depth_updates=int(payload.get("active_depth_updates", 0)),
        )


def write_shared_hub_status(repo_root: Path, status: SharedHubStatus) -> Path:
    """Persist hub status for external health checks."""
    path = status_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(status.to_json(), encoding="utf-8")
    return path


def load_shared_hub_status(repo_root: Path) -> SharedHubStatus | None:
    """Load hub status when present."""
    path = status_path(repo_root)
    if not path.exists():
        return None
    return SharedHubStatus.from_json(path.read_text(encoding="utf-8"))
