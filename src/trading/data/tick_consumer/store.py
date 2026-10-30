"""Persist ticks and one-minute bars under data/platter/."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from trading.data.platter_pipeline.io import atomic_parquet_append
from trading.data.tick_consumer.models import Bar1m, SignedTick, TickEvent

__all__ = ["TickStore", "tick_parquet_path", "bars_parquet_path"]


def tick_parquet_path(repo_root: Path, session_date: date) -> Path:
    return (
        repo_root
        / "data"
        / "platter"
        / "ticks"
        / session_date.isoformat()
        / "ticks.parquet"
    )


def bars_parquet_path(repo_root: Path, session_date: date) -> Path:
    return (
        repo_root
        / "data"
        / "platter"
        / "bars_1m"
        / session_date.isoformat()
        / "bars.parquet"
    )


class TickStore:
    """Append ticks and bars to session parquet files."""

    def __init__(self, repo_root: Path) -> None:
        self._root = repo_root
        self._tick_buffer: list[dict[str, object]] = []
        self._bar_buffer: list[dict[str, object]] = []

    def append_tick(self, tick: TickEvent | SignedTick, *, session_date: date) -> None:
        """Buffer one tick row."""
        row = tick.model_dump(mode="json")
        if "sign" in row:
            row["sign"] = str(row["sign"])
        self._tick_buffer.append(row)
        if len(self._tick_buffer) >= 500:
            self.flush_ticks(session_date=session_date)

    def append_bar(self, bar: Bar1m, *, session_date: date) -> None:
        """Buffer one bar row."""
        self._bar_buffer.append(bar.model_dump(mode="json"))
        if len(self._bar_buffer) >= 50:
            self.flush_bars(session_date=session_date)

    def flush_ticks(self, *, session_date: date) -> None:
        """Write buffered ticks to parquet."""
        if not self._tick_buffer:
            return
        path = tick_parquet_path(self._root, session_date)
        atomic_parquet_append(path, list(self._tick_buffer))
        self._tick_buffer.clear()

    def flush_bars(self, *, session_date: date) -> None:
        """Write buffered bars to parquet."""
        if not self._bar_buffer:
            return
        path = bars_parquet_path(self._root, session_date)
        atomic_parquet_append(path, list(self._bar_buffer))
        self._bar_buffer.clear()

    def write_health(self, session_date: date, payload: dict[str, object]) -> None:
        """Optional JSONL health line for websocket status."""
        path = (
            self._root
            / "data"
            / "platter"
            / "ticks"
            / session_date.isoformat()
            / "health.jsonl"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload) + "\n")
