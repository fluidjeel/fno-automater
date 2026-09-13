"""Layer 1 canonical market events. Distinct from domain FeatureSnapshot."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

__all__ = ["CanonicalMarketEvent", "RawMarketCapture"]


@dataclass(frozen=True, slots=True)
class RawMarketCapture:
    """Immutable broker payload as received, before normalization."""

    capture_id: str
    provider: str
    endpoint: str
    received_at: datetime
    payload: dict[str, Any]
    http_status: int


@dataclass(frozen=True, slots=True)
class CanonicalMarketEvent:
    """Normalized event with full lineage (DATA_SPEC.md)."""

    event_id: str
    provider: str
    symbol: str
    event_type: str
    event_time: datetime
    source_time: datetime
    receive_time: datetime
    provider_sequence: int | None
    payload: dict[str, Any]
    raw_ref: str
    normalization_version: str
