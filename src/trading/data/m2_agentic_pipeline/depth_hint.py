"""Share latest depth top-of-book with the tick consumer process."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from trading.data.m2_agentic_pipeline.io import atomic_json_write

__all__ = ["depth_hint_path", "publish_depth_hint", "read_depth_hint"]


def depth_hint_path(repo_root: Path) -> Path:
    return repo_root / "data" / "m2_agentic" / "state" / "latest_depth_hint.json"


def publish_depth_hint(
    repo_root: Path,
    *,
    bid: str,
    ask: str,
    as_of_utc: datetime,
) -> None:
    """Write hint atomically for tick signing."""
    atomic_json_write(
        depth_hint_path(repo_root),
        {
            "bid_price_1": bid,
            "ask_price_1": ask,
            "as_of_utc": as_of_utc.isoformat(),
        },
    )


def read_depth_hint(repo_root: Path) -> dict[str, Any] | None:
    path = depth_hint_path(repo_root)
    if not path.is_file():
        return None
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
