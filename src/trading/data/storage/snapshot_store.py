"""Append-only record of every decision cycle, accepted or rejected.

Rebuilding a snapshot from canonical events recovers the inputs but not the
verdict. A rejected cycle's DataQualityReport is the only artefact that explains
why no trade occurred, and it is discarded the moment the cycle ends, so it is
persisted here rather than recomputed later against a changed code version.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from trading.domain.contracts import DataQualityReport, FeatureSnapshot
from trading.domain.contracts.base import (
    NonEmptyStr,
    UtcDatetime,
    VersionedModel,
)

__all__ = ["SnapshotRecord", "SnapshotStore"]


class SnapshotRecord(VersionedModel):
    """One cycle's outcome: the snapshot it produced, or why it produced none."""

    symbol: NonEmptyStr
    as_of: UtcDatetime
    decision: Literal["SNAPSHOT", "REJECTED"]
    quality: DataQualityReport
    event_ids: tuple[NonEmptyStr, ...]
    snapshot: FeatureSnapshot | None = None

    @classmethod
    def for_cycle(
        cls,
        *,
        symbol: str,
        as_of: datetime,
        quality: DataQualityReport,
        event_ids: tuple[str, ...],
        snapshot: FeatureSnapshot | None,
    ) -> SnapshotRecord:
        return cls(
            symbol=symbol,
            as_of=as_of,
            decision="SNAPSHOT" if snapshot is not None else "REJECTED",
            quality=quality,
            event_ids=event_ids,
            snapshot=snapshot,
        )

    @property
    def snapshot_id(self) -> str | None:
        return None if self.snapshot is None else self.snapshot.snapshot_id


class SnapshotStore:
    """Daily JSONL of decision records. JSONL stays the authority."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def path_for(self, as_of: datetime) -> Path:
        day = as_of.astimezone(UTC).strftime("%Y-%m-%d")
        return self._root / f"{day}.jsonl"

    def append(self, record: SnapshotRecord) -> Path:
        path = self.path_for(record.as_of)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(record.model_dump_json() + "\n")
        return path

    def read(
        self,
        *,
        symbol: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> tuple[SnapshotRecord, ...]:
        records: list[SnapshotRecord] = []
        for path in sorted(self._root.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                text = line.strip()
                if not text:
                    continue
                record = SnapshotRecord.model_validate_json(text)
                if symbol is not None and record.symbol != symbol:
                    continue
                if start is not None and record.as_of < start:
                    continue
                if end is not None and record.as_of > end:
                    continue
                records.append(record)
        records.sort(key=lambda item: item.as_of)
        return tuple(records)

    def index_rows(self, record: SnapshotRecord) -> dict[str, object]:
        """Flat metadata for the derived catalog; the JSONL keeps the payload."""
        return {
            "snapshot_id": record.snapshot_id,
            "symbol": record.symbol,
            "as_of": record.as_of.astimezone(UTC).isoformat(),
            "decision": record.decision,
            "quality_state": str(record.quality.state),
            "permits_new_exposure": record.quality.permits_new_exposure,
            "reason_codes": json.dumps(
                [str(code) for code in record.quality.reason_codes]
            ),
        }
