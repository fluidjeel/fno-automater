"""Isolated CAS depth storage. No DuckDB or execution DB access."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path
from typing import Any

from trading.data.cas_depth.contracts import (
    CasDepthFeatureSnapshot,
    NormalizedDepthUpdate,
)

__all__ = ["CasDepthStorage"]


class CasDepthStorage:
    """Append-only JSONL partitions under ``data/cas_depth/``."""

    def __init__(self, root: Path, *, sample_every_n: int = 10) -> None:
        self._root = root
        self._raw_dir = root / "raw"
        self._normalized_dir = root / "normalized"
        self._snapshot_dir = root / "snapshots"
        self._sample_every_n = sample_every_n
        self._message_counts: dict[str, int] = {}
        self._buffers: dict[str, list[dict[str, Any]]] = {}
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        self._normalized_dir.mkdir(parents=True, exist_ok=True)
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def append_normalized(
        self,
        update: NormalizedDepthUpdate,
        *,
        flush: bool = False,
    ) -> None:
        """Buffer normalized updates and batch-flush to daily JSONL."""
        day = update.receive_timestamp.date().isoformat()
        path_key = f"normalized/{day}"
        row = update.model_dump(mode="json")
        self._buffers.setdefault(path_key, []).append(row)
        count = self._message_counts.get(update.symbol, 0) + 1
        self._message_counts[update.symbol] = count
        if count % self._sample_every_n == 0:
            self._append_raw_sample(update)
        batch_limit = 100
        if flush or len(self._buffers[path_key]) >= batch_limit:
            self._flush_buffer(path_key)

    def append_snapshot(self, snapshot: CasDepthFeatureSnapshot) -> None:
        """Write one bounded feature snapshot per symbol."""
        day = snapshot.receive_timestamp.date().isoformat()
        path = self._snapshot_dir / day / f"{snapshot.symbol.replace(':', '_')}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(snapshot.model_dump(mode="json")) + "\n")
        latest = self._snapshot_dir / "latest" / f"{snapshot.symbol.replace(':', '_')}.json"
        latest.parent.mkdir(parents=True, exist_ok=True)
        latest.write_text(
            json.dumps(snapshot.model_dump(mode="json"), indent=2),
            encoding="utf-8",
        )

    def flush_all(self) -> None:
        for path_key in list(self._buffers):
            self._flush_buffer(path_key)

    def _flush_buffer(self, path_key: str) -> None:
        rows = self._buffers.pop(path_key, [])
        if not rows:
            return
        subdir, day = path_key.split("/", maxsplit=1)
        path = self._root / subdir / f"{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")

    def _append_raw_sample(self, update: NormalizedDepthUpdate) -> None:
        day = update.receive_timestamp.date().isoformat()
        path = self._raw_dir / day / f"{update.symbol.replace(':', '_')}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(update.model_dump(mode="json")) + "\n")


class SnapshotRing:
    """Bounded in-memory ring of recent snapshots per symbol."""

    def __init__(self, max_per_symbol: int) -> None:
        self._max = max_per_symbol
        self._rings: dict[str, deque[NormalizedDepthUpdate]] = {}

    def push(self, update: NormalizedDepthUpdate) -> NormalizedDepthUpdate | None:
        ring = self._rings.setdefault(update.symbol, deque(maxlen=self._max))
        prior = ring[-1] if ring else None
        ring.append(update)
        return prior
