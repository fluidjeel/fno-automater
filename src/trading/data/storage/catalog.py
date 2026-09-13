"""Derived Parquet/DuckDB catalog. JSONL remains the replay authority."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC
from pathlib import Path
from typing import Any

from trading.data.events import CanonicalMarketEvent

__all__ = ["CatalogWriter"]


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
            import duckdb
            import polars as pl
        except ImportError:
            return None
        day = events[0].receive_time.astimezone(UTC).strftime("%Y-%m-%d")
        path = self._parquet / f"{day}.parquet"
        incoming = pl.DataFrame([_event_row(event) for event in events])
        if path.is_file():
            existing = pl.read_parquet(path)
            merged = pl.concat([existing, incoming], how="vertical").unique(
                subset=["event_id"],
                keep="last",
            )
        else:
            merged = incoming
        merged.write_parquet(path)
        incoming_path = self._parquet / f"{day}.incoming.parquet"
        incoming.write_parquet(incoming_path)
        connection = duckdb.connect(str(self._duckdb_path))
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS canonical_events ("
                "event_id VARCHAR PRIMARY KEY, provider VARCHAR, symbol VARCHAR, "
                "event_type VARCHAR, event_time VARCHAR, source_time VARCHAR, "
                "receive_time VARCHAR, provider_sequence BIGINT, raw_ref VARCHAR, "
                "normalization_version VARCHAR)"
            )
            quoted = str(incoming_path).replace("'", "''")
            connection.execute(
                "INSERT OR REPLACE INTO canonical_events "
                f"SELECT * FROM read_parquet('{quoted}')"
            )
        finally:
            connection.close()
            incoming_path.unlink(missing_ok=True)
        return path
