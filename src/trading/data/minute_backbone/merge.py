"""Merge chain, depth, and quotes into one minute bundle."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trading.data.minute_backbone.models import (
    ChainSnapshot,
    DepthSnapshot,
    MinuteBundle,
    MinuteStatus,
    minute_bundle_path,
)
from trading.data.platter_pipeline.io import atomic_json_write

_IST = ZoneInfo("Asia/Kolkata")

__all__ = ["merge_minute_bundle", "persist_minute_bundle"]


def merge_minute_bundle(
    *,
    minute_start_ist: datetime,
    chain: ChainSnapshot,
    depth: DepthSnapshot,
    nifty_quote: dict[str, object],
    vix_quote: dict[str, object],
    status: MinuteStatus = MinuteStatus.OK,
) -> MinuteBundle:
    """Build MinuteBundle JSON contract."""
    return MinuteBundle(
        minute_start_ist=minute_start_ist,
        status=status,
        nifty_quote=nifty_quote,
        vix_quote=vix_quote,
        chain_snapshot_id=chain.snapshot_id,
        depth_snapshot_id=depth.snapshot_id,
        chain=chain.model_dump(mode="json"),
        depth=depth.model_dump(mode="json"),
    )


def persist_minute_bundle(repo_root: Path, bundle: MinuteBundle) -> Path:
    """Write bundle atomically under data/platter/minute/."""
    local = bundle.minute_start_ist.astimezone(_IST)
    session_date = local.date().isoformat()
    hhmm = local.strftime("%H%M")
    path = minute_bundle_path(repo_root, session_date, hhmm)
    atomic_json_write(path, bundle.model_dump(mode="json"))
    return path
