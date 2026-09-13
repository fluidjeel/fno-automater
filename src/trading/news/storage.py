"""Append-only, idempotent JSONL storage for news pipeline records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from trading.news.contracts import (
    EventRiskState,
    MacroProposal,
    NewsEvent,
    NewsItem,
    SentimentSnapshot,
)
from trading.news.sources import CollectionBatch

__all__ = ["NewsJsonlStore"]

Record = TypeVar("Record", bound=BaseModel)


class NewsJsonlStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _append(self, name: str, records: tuple[Record, ...], key: str) -> int:
        path = self.root / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing: set[str] = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                    value = self._identity(row, key)
                    if value:
                        existing.add(value)
                except json.JSONDecodeError:
                    continue
        count = 0
        with path.open("a", encoding="utf-8") as handle:
            for record in records:
                identity = self._identity(record.model_dump(mode="json"), key)
                if not identity:
                    continue
                if identity in existing:
                    continue
                handle.write(record.model_dump_json() + "\n")
                existing.add(identity)
                count += 1
        return count

    @staticmethod
    def _identity(record: dict[str, object], key: str) -> str:
        if key == "risk":
            raw_event_ids = record.get("event_ids", [])
            event_ids = (
                ",".join(str(value) for value in raw_event_ids)
                if isinstance(raw_event_ids, list)
                else ""
            )
            return "|".join(
                (
                    str(record.get("scope", "")),
                    str(record.get("as_of", "")),
                    event_ids,
                )
            )
        value = record.get(key)
        return value if isinstance(value, str) else ""

    def append_items(self, records: tuple[NewsItem, ...]) -> int:
        return self._append("items", records, "news_item_id")

    def append_events(self, records: tuple[NewsEvent, ...]) -> int:
        return self._append("events", records, "event_id")

    def append_snapshot(self, record: SentimentSnapshot) -> int:
        return self._append("snapshots", (record,), "snapshot_id")

    def append_risks(self, records: tuple[EventRiskState, ...]) -> int:
        return self._append("risks", records, "risk")

    def append_proposal(self, record: MacroProposal) -> int:
        return self._append("proposals", (record,), "proposal_id")

    def append_cycle(self, batch: CollectionBatch) -> int:
        """Persist source health and audit correlation for a collection batch."""
        cycle_id = batch.cycle_id
        path = self.root / "cycles.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    if json.loads(line).get("cycle_id") == cycle_id:
                        return 0
                except json.JSONDecodeError:
                    continue
        health = [
            {
                "source_id": item.source_id,
                "status": item.status,
                "fetched_count": item.fetched_count,
                "invalid_count": item.invalid_count,
                "reason": item.reason,
                "retry_count": item.retry_count,
                "as_of": item.as_of.isoformat(),
            }
            for item in batch.source_health
        ]
        record = {
            "cycle_id": cycle_id,
            "as_of": batch.as_of.isoformat(),
            "source_health": health,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return 1

    def latest_cycle(self) -> dict[str, object] | None:
        path = self.root / "cycles.jsonl"
        if not path.exists():
            return None
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                return record
        return None

    def count(self, name: str) -> int:
        path = self.root / f"{name}.jsonl"
        return (
            sum(
                1
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            if path.exists()
            else 0
        )
