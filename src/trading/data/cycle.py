"""The single snapshot-emission decision, shared by the live and replay paths."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from trading.data.events import CanonicalMarketEvent
from trading.data.snapshot_builder import MarketSnapshotBuilder
from trading.domain.contracts import DataQualityReport, FeatureSnapshot
from trading.domain.enums import DataQuality

__all__ = ["build_cycle_snapshot"]


def build_cycle_snapshot(
    events: Sequence[CanonicalMarketEvent],
    *,
    quality: DataQualityReport,
    as_of: datetime,
    builder: MarketSnapshotBuilder,
) -> FeatureSnapshot | None:
    """Emit a snapshot unless the cycle is INVALID.

    STALE and DEGRADED snapshots are emitted with their quality preserved so a
    replay reproduces the same information stream the live path saw. Refusing
    new exposure is Layer 2's decision via `permits_new_exposure`; discarding
    the evidence here would make replay disagree with live.
    """
    if quality.state is DataQuality.INVALID:
        return None
    return builder.build(events, as_of=as_of, quality=quality)
