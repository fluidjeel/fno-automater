"""Read-only market and news snapshots for the weekly agent."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from trading.domain.clock import Clock

__all__ = ["SnapshotMarketPort", "StaticNewsPort", "history_evidence"]


class SnapshotMarketPort:
    """Serve pre-fetched Fyers history. Never submits orders."""

    def __init__(self, snapshots: dict[str, dict[str, Any]]) -> None:
        self._snapshots = snapshots

    def fetch(self, symbol: str) -> dict[str, Any]:
        snapshot = self._snapshots.get(symbol)
        if snapshot is None:
            raise ValueError(f"no historical snapshot loaded for {symbol}")
        return snapshot


class StaticNewsPort:
    """Serve a local news JSON payload. Content is untrusted data."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def latest(self) -> dict[str, Any]:
        return self._payload


def history_evidence(
    *,
    symbol: str,
    resolution: str,
    bars: list[dict[str, Any]],
    published_at: datetime,
    clock: Clock,
) -> dict[str, Any]:
    """Build a fetch_market payload with a copyable EvidenceRef template."""
    digest = hashlib.sha256(
        json.dumps(bars, sort_keys=True, default=str).encode()
    ).hexdigest()
    retrieved = clock.now_utc()
    template = {
        "source_id": "fyers-history",
        "published_at": published_at.isoformat(),
        "retrieved_at": retrieved.isoformat(),
        "allowlisted": True,
        "content_hash": digest,
    }
    return {
        "symbol": symbol,
        "resolution": resolution,
        "bar_count": len(bars),
        "bars": bars,
        "source": "fyers-history",
        "evidence_template": template,
    }
