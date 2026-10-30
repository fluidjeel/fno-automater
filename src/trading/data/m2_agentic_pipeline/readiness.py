"""Wait for minute pipeline outputs before the five-minute agent decides."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading.data.m2_agentic.assemble import latest_feature_row, load_minute_bundle_for_mark

_IST = ZoneInfo("Asia/Kolkata")

__all__ = ["align_mark_to_minute", "wait_for_minute_pipeline"]


def align_mark_to_minute(mark: datetime) -> datetime:
    """Floor an instant to the IST minute boundary."""
    local = mark.astimezone(_IST)
    return local.replace(second=0, microsecond=0)


def _feature_matches_mark(feature_row: dict[str, Any], minute_mark: datetime) -> bool:
    try:
        row_start = datetime.fromisoformat(str(feature_row["minute_start_ist"]))
    except (KeyError, ValueError):
        return False
    return align_mark_to_minute(row_start) == minute_mark


def wait_for_minute_pipeline(
    repo_root: Path,
    mark: datetime,
    *,
    max_lag_seconds: int,
    poll_interval_seconds: float = 0.25,
) -> tuple[dict[str, Any], Path] | None:
    """Poll until feature row and minute bundle exist for the mark minute."""
    minute_mark = align_mark_to_minute(mark)
    deadline = time.monotonic() + max(0, max_lag_seconds)
    while True:
        bundle = load_minute_bundle_for_mark(repo_root, minute_mark)
        loaded = latest_feature_row(repo_root, minute_mark)
        if bundle is not None and loaded is not None:
            feature_row, feature_path = loaded
            if _feature_matches_mark(feature_row, minute_mark):
                return feature_row, feature_path
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_interval_seconds)
