"""Derived Parquet/DuckDB catalog. JSONL remains the replay authority."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC
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
        if path.is_file():
            existing = self._normalize_event_index(pl.read_parquet(path))
            merged = pl.concat([existing, incoming], how="vertical").unique(
                subset=["event_id"],
                keep="last",
            )
        else:
            merged = incoming
        merged.write_parquet(path)
        incoming_path = self._parquet / f"{day}.incoming.parquet"
        incoming.write_parquet(incoming_path)
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
        if path.is_file():
            existing = self._normalize_snapshot_index(pl.read_parquet(path))
            merged = pl.concat([existing, incoming], how="vertical").unique(
                subset=["as_of", "symbol"],
                keep="last",
            )
        else:
            merged = incoming
        merged.write_parquet(path)
        incoming_path = self._parquet / f"snapshots-{day}.incoming.parquet"
        incoming.write_parquet(incoming_path)
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
