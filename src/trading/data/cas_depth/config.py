"""Validated CAS depth collector configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["CasDataConfig", "load_cas_data_config"]


class CasStorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root: str = "data/cas_depth"
    raw_subdir: str = "raw"
    normalized_subdir: str = "normalized"
    snapshot_subdir: str = "snapshots"
    metrics_subdir: str = "metrics"
    sample_every_n: int = Field(default=10, ge=1)


class CasSymbolsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index_or_future: str
    options: tuple[str, ...] = ()
    stock_option_underlying: str = "NSE:RELIANCE-EQ"
    mcx: tuple[str, ...] = ("MCX:GOLDM", "MCX:CRUDEOILM")
    max_options: int = Field(default=2, ge=0, le=5)


class CasCollectorConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    queue_maxsize: int = Field(default=5000, ge=100)
    batch_size: int = Field(default=100, ge=1)
    batch_flush_seconds: float = Field(default=2.0, gt=0)
    max_stale_seconds: float = Field(default=5.0, gt=0)
    reconnect_attempts: int = Field(default=5, ge=1)
    reconnect_backoff_seconds: float = Field(default=2.0, gt=0)
    tbt_channel: str = "1"
    data_ws_channel: int = Field(default=11, ge=1)
    snapshot_publish_interval_seconds: float = Field(default=1.0, gt=0)
    max_snapshots_per_symbol: int = Field(default=100, ge=1)


class CasResourceLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_memory_mb: int = Field(default=512, ge=64)
    restart_on_memory_exceeded: bool = True
    restart_max_attempts: int = Field(default=3, ge=0)
    restart_backoff_seconds: float = Field(default=5.0, gt=0)


class CasBenchmarkConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    duration_minutes: int = Field(default=30, ge=1, le=120)
    report_subdir: str = "benchmarks"


class CasDataConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    cas_data_mode: Literal["DEPTH_ONLY"] = "DEPTH_ONLY"
    cas_live_orders: Literal[False] = False
    storage: CasStorageConfig = CasStorageConfig()
    symbols: CasSymbolsConfig
    collector: CasCollectorConfig = CasCollectorConfig()
    resource_limits: CasResourceLimits = CasResourceLimits()
    benchmark: CasBenchmarkConfig = CasBenchmarkConfig()


def load_cas_data_config(path: Path) -> CasDataConfig:
    """Load and validate ``config/cas_data.yaml``."""
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    symbols = raw.get("symbols", {})
    if isinstance(symbols.get("options"), list):
        symbols["options"] = tuple(symbols["options"])
    if isinstance(symbols.get("mcx"), list):
        symbols["mcx"] = tuple(symbols["mcx"])
    raw["symbols"] = symbols
    return CasDataConfig.model_validate(raw)
