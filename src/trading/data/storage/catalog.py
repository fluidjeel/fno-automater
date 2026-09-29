"""Derived Parquet/DuckDB catalog. JSONL remains the replay authority."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading.data.events import CanonicalMarketEvent

__all__ = ["CatalogWriter"]

logger = logging.getLogger(__name__)

_SNAPSHOT_INDEX_COLUMNS: tuple[tuple[str, type], ...] = (
    ("snapshot_id", str),
    ("symbol", str),
    ("as_of", str),
    ("decision", str),
    ("quality_state", str),
    ("permits_new_exposure", bool),
    ("reason_codes", str),
)

_EVENT_INDEX_COLUMNS: tuple[tuple[str, type], ...] = (
    ("event_id", str),
    ("provider", str),
    ("symbol", str),
    ("event_type", str),
    ("event_time", str),
    ("source_time", str),
    ("receive_time", str),
    ("provider_sequence", int),
    ("raw_ref", str),
    ("normalization_version", str),
)


def _fsync_directory(directory: Path) -> None:
    """Persist a rename in ``directory``; best effort where unsupported."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_parquet(frame: Any, path: Path) -> None:
    """Write ``frame`` to ``path`` so readers never observe a partial file.

    Writes a unique temp file in the same directory, fsyncs it, then
    ``os.replace``s it over the target. On failure the target is untouched and
    the temp file is removed. The temp name ends in ``.tmp`` so ``*.parquet``
    globs never pick it up.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        frame.write_parquet(tmp)
        with tmp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)


def _quarantine_corrupt(path: Path, exc: BaseException) -> None:
    """Move an unreadable derived Parquet file aside instead of crashing."""
    # Stamp with the torn file's own mtime (when it was damaged); production
    # code never reads ambient wall time (Invariant 21).
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        stamp = mtime.strftime("%Y%m%dT%H%M%SZ")
    except OSError:
        stamp = "unknown"
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    suffix = 1
    while target.exists():
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{suffix}")
        suffix += 1
    try:
        os.replace(path, target)
    except OSError as move_exc:
        logger.warning(
            "catalog parquet %s unreadable (%s); could not move aside: %s",
            path,
            exc,
            move_exc,
        )
        return
    logger.warning(
        "catalog parquet %s unreadable (%s); moved aside to %s and continuing "
        "with incoming rows only",
        path,
        exc,
        target,
    )


def _read_existing(path: Path, normalize: Callable[[Any], Any]) -> Any | None:
    """Return the normalized existing frame, or ``None`` if absent or corrupt.

    The catalog is derived (JSONL stays the replay authority), so a torn file
    must never take the decision cycle down with it.
    """
    import polars as pl

    if not path.is_file():
        return None
    try:
        raw = pl.read_parquet(path)
    except (Exception, pl.exceptions.PanicException) as exc:
        _quarantine_corrupt(path, exc)
        return None
    return normalize(raw)


def _event_row(event: CanonicalMarketEvent) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "provider": event.provider,
        "symbol": event.symbol,
        "event_type": event.event_type,
        "event_time": event.event_time.astimezone(UTC).isoformat(),
        "source_time": event.source_time.astimezone(UTC).isoformat(),
        "receive_time": event.receive_time.astimezone(UTC).isoformat(),
        "provider_sequence": event.provider_sequence,
        "raw_ref": event.raw_ref,
        "normalization_version": event.normalization_version,
    }


class CatalogWriter:
    """Append canonical event metadata to Parquet and DuckDB."""

    def __init__(
        self,
        root: Path,
        *,
        duckdb_path: Path,
        parquet_subdir: str = "parquet",
    ) -> None:
        self._root = root
        self._parquet = root / parquet_subdir
        self._duckdb_path = duckdb_path
        self._parquet.mkdir(parents=True, exist_ok=True)
        self._duckdb_path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, events: Sequence[CanonicalMarketEvent]) -> Path | None:
        """Write one Parquet file and upsert event_ids into DuckDB."""
        if not events:
            return None
        try:
            import polars as pl
        except ImportError:
            return None
        day = events[0].receive_time.astimezone(UTC).strftime("%Y-%m-%d")
        path = self._parquet / f"{day}.parquet"
        incoming = self._normalize_event_index(
            pl.DataFrame([_event_row(event) for event in events])
        )
        existing = _read_existing(path, self._normalize_event_index)
        if existing is not None:
            merged = pl.concat([existing, incoming], how="vertical").unique(
                subset=["event_id"],
                keep="last",
            )
        else:
            merged = incoming
        _atomic_write_parquet(merged, path)
        incoming_path = self._parquet / f"{day}.incoming.parquet"
        _atomic_write_parquet(incoming, incoming_path)
        self._upsert_duckdb(
            incoming_path,
            (
                "CREATE TABLE IF NOT EXISTS canonical_events ("
                "event_id VARCHAR PRIMARY KEY, provider VARCHAR, symbol VARCHAR, "
                "event_type VARCHAR, event_time VARCHAR, source_time VARCHAR, "
                "receive_time VARCHAR, provider_sequence BIGINT, raw_ref VARCHAR, "
                "normalization_version VARCHAR)"
            ),
            "canonical_events",
        )
        return path

    def _normalize_frame(
        self,
        frame: Any,
        schema: tuple[tuple[str, type], ...],
    ) -> Any:
        """Align catalog rows so legacy Null-typed columns concat with new values."""
        import polars as pl

        row_count = frame.height
        columns: dict[str, Any] = {}
        for name, py_type in schema:
            dtype: Any
            if py_type is bool:
                dtype = pl.Boolean
            elif py_type is int:
                dtype = pl.Int64
            else:
                dtype = pl.Utf8
            if name in frame.columns:
                columns[name] = frame[name].cast(dtype)
            else:
                columns[name] = pl.Series(name, [None] * row_count, dtype=dtype)
        return pl.DataFrame(columns)

    def _normalize_snapshot_index(self, frame: Any) -> Any:
        """Align snapshot-index rows before concat."""
        return self._normalize_frame(frame, _SNAPSHOT_INDEX_COLUMNS)

    def _normalize_event_index(self, frame: Any) -> Any:
        """Align canonical-event rows before concat."""
        return self._normalize_frame(frame, _EVENT_INDEX_COLUMNS)

    def append_snapshots(self, rows: Sequence[Mapping[str, Any]]) -> Path | None:
        """Index decision-cycle metadata. The snapshot JSONL keeps the payload."""
        if not rows:
            return None
        try:
            import polars as pl
        except ImportError:
            return None
        first_as_of = str(rows[0]["as_of"])
        day = first_as_of[:10]
        path = self._parquet / f"snapshots-{day}.parquet"
        incoming = self._normalize_snapshot_index(
            pl.DataFrame([dict(row) for row in rows])
        )
        existing = _read_existing(path, self._normalize_snapshot_index)
        if existing is not None:
            merged = pl.concat([existing, incoming], how="vertical").unique(
                subset=["as_of", "symbol"],
                keep="last",
            )
        else:
            merged = incoming
        _atomic_write_parquet(merged, path)
        incoming_path = self._parquet / f"snapshots-{day}.incoming.parquet"
        _atomic_write_parquet(incoming, incoming_path)
        self._upsert_duckdb(
            incoming_path,
            (
                "CREATE TABLE IF NOT EXISTS decision_snapshots ("
                "snapshot_id VARCHAR, symbol VARCHAR, as_of VARCHAR, "
                "decision VARCHAR, quality_state VARCHAR, "
                "permits_new_exposure BOOLEAN, reason_codes VARCHAR, "
                "PRIMARY KEY (symbol, as_of))"
            ),
            "decision_snapshots",
        )
        return path

    def _upsert_duckdb(
        self,
        incoming_path: Path,
        create_sql: str,
        table_name: str,
    ) -> None:
        """Best-effort DuckDB index. Parquet JSONL remain authoritative."""
        try:
            import duckdb
        except ImportError:
            incoming_path.unlink(missing_ok=True)
            return
        try:
            connection = duckdb.connect(str(self._duckdb_path))
            try:
                connection.execute(create_sql)
                quoted = str(incoming_path).replace("'", "''")
                connection.execute(
                    f"INSERT OR REPLACE INTO {table_name} "
                    f"SELECT * FROM read_parquet('{quoted}')"
                )
            finally:
                connection.close()
        except Exception as exc:
            logger.warning("duckdb catalog index skipped: %s", exc)
        finally:
            incoming_path.unlink(missing_ok=True)
