"""Once-per-minute chain, depth, and quote bundle."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

__all__ = [
    "ChainSnapshot",
    "ChainStrikeRow",
    "DepthSnapshot",
    "MinuteBundle",
    "MinuteStatus",
    "minute_bundle_path",
]


class MinuteStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class ChainStrikeRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    strike: str
    option_type: Literal["CALL", "PUT"]
    ltp: str
    bid: str
    ask: str
    oi: int
    volume: int
    iv: str
    delta: str
    gamma: str
    vega: str
    theta: str
    symbol: str


class ChainSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    snapshot_id: str
    symbol: str
    expiry_date: str
    as_of_utc: datetime
    strikes: tuple[ChainStrikeRow, ...]


class DepthSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    snapshot_id: str
    symbol: str
    as_of_utc: datetime
    bid_price_1: str
    bid_size_1: int
    bid_price_2: str
    bid_size_2: int
    bid_price_3: str
    bid_size_3: int
    bid_price_4: str
    bid_size_4: int
    bid_price_5: str
    bid_size_5: int
    ask_price_1: str
    ask_size_1: int
    ask_price_2: str
    ask_size_2: int
    ask_price_3: str
    ask_size_3: int
    ask_price_4: str
    ask_size_4: int
    ask_price_5: str
    ask_size_5: int
    spread_bps: str


class MinuteBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    minute_start_ist: datetime
    status: MinuteStatus
    nifty_quote: dict[str, Any]
    vix_quote: dict[str, Any]
    chain_snapshot_id: str
    depth_snapshot_id: str
    chain: dict[str, Any]
    depth: dict[str, Any]


def minute_bundle_path(repo_root: Path, session_date: str, hhmm: str) -> Path:
    return (
        repo_root
        / "data"
        / "m2_agentic"
        / "minute"
        / session_date
        / f"minute_{hhmm}.json"
    )


def snapshot_id(payload: dict[str, Any]) -> str:
    """Stable blake2 hash for snapshot identity."""
    raw = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.blake2b(raw, digest_size=16).hexdigest()
