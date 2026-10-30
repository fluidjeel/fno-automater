"""Persist ticks and one-minute bars under data/m2_agentic/ (append-only JSONL)."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from trading.data.m2_agentic_pipeline.io import atomic_jsonl_append
from trading.data.tick_consumer.models import Bar1m, SignedTick, TickEvent
from trading.data.tick_consumer.session_paths import (
    bars_jsonl_path,
    hour_ist_from_instant,
    session_bars_root,
    session_ticks_root,
    tick_jsonl_path,
)

__all__ = ["TickStore", "bars_parquet_path", "tick_parquet_path"]


def tick_parquet_path(repo_root: Path, session_date: date) -> Path:
    """Freshness marker path for tick shards (legacy name)."""
    return session_ticks_root(repo_root, session_date) / ".tick_store"


def bars_parquet_path(repo_root: Path, session_date: date) -> Path:
    """Freshness marker path for bar shards (legacy name)."""
    return session_bars_root(repo_root, session_date) / ".bar_store"


class TickStore:
    """Append ticks and bars to hourly JSONL shards."""

    def __init__(self, repo_root: Path) -> None:
        self._root = repo_root
        self._tick_buffer: list[dict[str, object]] = []
        self._bar_buffer: list[Bar1m] = []

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
        self._bar_buffer.append(bar)
        if len(self._bar_buffer) >= 50:
            self.flush_bars(session_date=session_date)

    def _hour_from_tick_rows(self, rows: list[dict[str, object]], *, session_date: date) -> int:
        for row in rows:
            raw = row.get("received_at")
            if raw is None:
                continue
            try:
                return hour_ist_from_instant(datetime.fromisoformat(str(raw)))
            except ValueError:
                continue
        return hour_ist_from_instant(datetime.now().astimezone())

    def flush_ticks(self, *, session_date: date) -> None:
        """Write buffered ticks to hourly JSONL without reading prior shards."""
        if not self._tick_buffer:
            return
        hour = self._hour_from_tick_rows(self._tick_buffer, session_date=session_date)
        path = tick_jsonl_path(self._root, session_date, hour)
        atomic_jsonl_append(path, list(self._tick_buffer))
        marker = tick_parquet_path(self._root, session_date)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"updated_utc": datetime.now().isoformat()}), encoding="utf-8")
        self._tick_buffer.clear()

    def flush_bars(self, *, session_date: date) -> None:
        """Write buffered bars and update incremental bar stats."""
        if not self._bar_buffer:
            return
        rows = [bar.model_dump(mode="json") for bar in self._bar_buffer]
        hour = hour_ist_from_instant(self._bar_buffer[-1].bar_start_ist)
        path = bars_jsonl_path(self._root, session_date, hour)
        atomic_jsonl_append(path, rows)
        from trading.data.features.bar_stats_cache import apply_bars

        apply_bars(self._root, list(self._bar_buffer), session_date=session_date.isoformat())
        marker = bars_parquet_path(self._root, session_date)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"updated_utc": datetime.now().isoformat()}), encoding="utf-8")
        self._bar_buffer.clear()

    def write_health(self, session_date: date, payload: dict[str, object]) -> None:
        """Optional JSONL health line for websocket status."""
        path = session_ticks_root(self._root, session_date) / "health.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
