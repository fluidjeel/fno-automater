"""Append-only event storage. JSONL for tests and first VM bootstrap."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trading.data.events import CanonicalMarketEvent, RawMarketCapture

__all__ = ["JsonlEventStore"]


def _dt_to_str(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _dt_from_str(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("stored timestamp is naive")
    return parsed.astimezone(UTC)


class JsonlEventStore:
    """Simple append-only store for offline tests and first VM bootstrap."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._raw = root / "raw"
        self._canonical = root / "canonical"
        self._raw.mkdir(parents=True, exist_ok=True)
        self._canonical.mkdir(parents=True, exist_ok=True)

    def append_raw(self, capture: RawMarketCapture) -> Path:
        day = capture.received_at.astimezone(UTC).strftime("%Y-%m-%d")
        directory = self._raw / "fyers" / day
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{capture.capture_id}.json"
        path.write_text(
            json.dumps(
                {
                    "capture_id": capture.capture_id,
                    "provider": capture.provider,
                    "endpoint": capture.endpoint,
                    "received_at": _dt_to_str(capture.received_at),
                    "http_status": capture.http_status,
                    "payload": capture.payload,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    def append_canonical(self, event: CanonicalMarketEvent) -> None:
        day = event.receive_time.astimezone(UTC).strftime("%Y-%m-%d")
        path = self._canonical / f"{day}.jsonl"
        record = {
            "event_id": event.event_id,
            "provider": event.provider,
            "symbol": event.symbol,
            "event_type": event.event_type,
            "event_time": _dt_to_str(event.event_time),
            "source_time": _dt_to_str(event.source_time),
            "receive_time": _dt_to_str(event.receive_time),
            "provider_sequence": event.provider_sequence,
            "payload": event.payload,
            "raw_ref": event.raw_ref,
            "normalization_version": event.normalization_version,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def read_canonical(
        self,
        *,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> list[CanonicalMarketEvent]:
        events: list[CanonicalMarketEvent] = []
        if not self._canonical.exists():
            return events
        for path in sorted(self._canonical.glob("*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                record: dict[str, Any] = json.loads(line)
                if record["symbol"] != symbol:
                    continue
                event_time = _dt_from_str(record["event_time"])
                if not start <= event_time <= end:
                    continue
                events.append(
                    CanonicalMarketEvent(
                        event_id=record["event_id"],
                        provider=record["provider"],
                        symbol=record["symbol"],
                        event_type=record["event_type"],
                        event_time=event_time,
                        source_time=_dt_from_str(record["source_time"]),
                        receive_time=_dt_from_str(record["receive_time"]),
                        provider_sequence=record.get("provider_sequence"),
                        payload=record["payload"],
                        raw_ref=record["raw_ref"],
                        normalization_version=record["normalization_version"],
                    )
                )
        events.sort(key=lambda item: item.event_time)
        return events
