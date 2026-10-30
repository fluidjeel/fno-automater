"""Canonical tick, bar, and delta shapes for the platter pipeline."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Bar1m",
    "DeltaState",
    "SignedTick",
    "TickEvent",
    "TickHealthEvent",
    "TickSign",
]


class TickSign(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    UNKNOWN = "UNKNOWN"


class TickEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    symbol: str
    ltp: str
    volume: int = 0
    trade_size: int = 0
    exchange_ts: datetime
    received_at: datetime
    source: Literal["fyers_ws"] = "fyers_ws"


class SignedTick(TickEvent):
    sign: TickSign
    sign_confidence: str = "1"


class Bar1m(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    symbol: str
    bar_start_ist: datetime
    open: str
    high: str
    low: str
    close: str
    volume: int
    buy_volume: int
    sell_volume: int
    unknown_volume: int
    tick_count: int


class DeltaState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_date: date
    session_delta: int = 0
    session_buy_volume: int = 0
    session_sell_volume: int = 0
    delta_5m: int = 0
    cumulative_delta: int = 0


class TickHealthEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    connected: bool
    at_utc: datetime
    detail: str = ""
