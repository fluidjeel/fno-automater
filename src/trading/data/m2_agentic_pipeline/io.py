"""Atomic, locked writes for M2 agentic pipeline artifacts."""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import polars as pl

__all__ = ["atomic_json_write", "atomic_jsonl_append", "atomic_parquet_append", "file_lock"]


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Exclusive lock on a sidecar lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("w", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def atomic_json_write(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    """Write JSON via temp file and atomic rename under lock."""
    with file_lock(path):
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(path)


def atomic_jsonl_append(path: Path, rows: list[dict[str, object]]) -> None:
    """Append JSON lines without reading existing content."""
    if not rows:
        return
    with file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False))
                handle.write("\n")


def atomic_parquet_append(path: Path, rows: list[dict[str, object]]) -> None:
    """Append rows to parquet with lock and temp replace (legacy path only)."""
    if not rows:
        return
    with file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = pl.DataFrame(rows)
        if path.is_file():
            existing = pl.read_parquet(path)
            frame = pl.concat([existing, frame], how="diagonal_relaxed")
        temporary = path.with_suffix(".parquet.tmp")
        frame.write_parquet(temporary)
        temporary.replace(path)
