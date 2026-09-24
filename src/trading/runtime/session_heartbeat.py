"""Crash-safe session liveness markers for dashboard observability."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["write_session_heartbeat"]


def write_session_heartbeat(path: Path, payload: dict[str, Any]) -> None:
    """Atomically persist one paper-session heartbeat snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp_path.replace(path)
