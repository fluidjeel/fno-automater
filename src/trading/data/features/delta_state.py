"""Persist session delta for feature computation."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from trading.data.platter_pipeline.io import atomic_json_write
from trading.data.tick_consumer.models import DeltaState

__all__ = ["delta_state_path", "load_delta_state", "save_delta_state"]


def delta_state_path(repo_root: Path, session_date: str) -> Path:
    return repo_root / "data" / "platter" / "state" / f"delta_{session_date}.json"


def load_delta_state(repo_root: Path, session_date: str) -> DeltaState | None:
    path = delta_state_path(repo_root, session_date)
    if not path.is_file():
        return None
    try:
        return DeltaState.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None


def save_delta_state(repo_root: Path, state: DeltaState) -> None:
    atomic_json_write(
        delta_state_path(repo_root, state.session_date.isoformat()),
        json.loads(state.model_dump_json()),
    )
