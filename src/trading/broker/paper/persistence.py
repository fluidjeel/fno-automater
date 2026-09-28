"""Atomic JSON persistence for PAPER broker_state.json."""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["atomic_write_json"]


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """Write JSON atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(path)
