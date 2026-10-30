"""Append-only tick and bar paths (hourly JSONL shards per session)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

__all__ = [
    "bars_jsonl_path",
    "latest_session_shard_mtime",
    "session_bars_root",
    "session_ticks_root",
    "tick_jsonl_path",
]


def session_ticks_root(repo_root: Path, session_date: date) -> Path:
    return repo_root / "data" / "m2_agentic" / "ticks" / session_date.isoformat()


def session_bars_root(repo_root: Path, session_date: date) -> Path:
    return repo_root / "data" / "m2_agentic" / "bars_1m" / session_date.isoformat()


def hour_ist_from_instant(instant: datetime) -> int:
    return instant.astimezone(_IST).hour


def tick_jsonl_path(repo_root: Path, session_date: date, hour_ist: int) -> Path:
    return session_ticks_root(repo_root, session_date) / f"h{hour_ist:02d}" / "ticks.jsonl"


def bars_jsonl_path(repo_root: Path, session_date: date, hour_ist: int) -> Path:
    return session_bars_root(repo_root, session_date) / f"h{hour_ist:02d}" / "bars.jsonl"


def latest_session_shard_mtime(root: Path) -> float | None:
    if not root.is_dir():
        return None
    newest: float | None = None
    for path in root.rglob("*.jsonl"):
        if not path.is_file():
            continue
        mtime = path.stat().st_mtime
        if newest is None or mtime > newest:
            newest = mtime
    return newest
