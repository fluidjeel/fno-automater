"""Validated data-pipeline configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from trading.domain.enums import AssetClass, Exchange, InstrumentKind

__all__ = [
    "DataPipelineConfig",
    "FyersPipelineConfig",
    "QualityConfig",
    "SessionConfig",
    "StorageConfig",
    "UnderlyingConfig",
    "load_data_pipeline_config",
]


class UnderlyingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    feature_set_version: str
    exchange: Exchange
    underlying: str
    instrument_kind: InstrumentKind
    asset_class: AssetClass


class StorageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    root: str = "data"
    raw_subdir: str = "raw"
    canonical_subdir: str = "canonical"
    duckdb_path: str = "data/catalog.duckdb"
    parquet_subdir: str = "parquet"


class SessionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timezone: str
    open_local: str
    close_local: str
    verified: bool = False
    segment: str = "NSE_FO"


class QualityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_bar_count: int = Field(default=1, ge=0)
    min_strike_count: int = Field(default=1, ge=0)
    max_clock_drift_ms: int = Field(default=5_000, ge=0)
    clock_drift_invalid: bool = False
    max_cross_source_bps: int = Field(default=50, ge=0)
    max_spread_bps: int = Field(default=50, ge=0)


class FyersPipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    option_chain_strike_count: int = Field(default=25, ge=1)
    poll_interval_seconds: int = Field(default=60, ge=1)
    bar_resolutions: tuple[str, ...] = ("5",)
    bar_lookback_days: int = Field(default=5, ge=1)
    chain_greeks: bool = True
    history_oi_flag: bool = True
    fetch_depth: bool = True
    fetch_market_status: bool = True
    fetch_expiry_dates: bool = True
    greeks_calculation_version: str = "fyers_chain"
    ws_channel: int = Field(default=11, ge=1)
    ws_max_ticks: int = Field(default=100, ge=1)
    ws_duration_seconds: int = Field(default=60, ge=1)
    ws_reconnect: bool = True
    ws_reconnect_attempts: int = Field(default=5, ge=1)
    ws_reconnect_backoff_seconds: float = Field(default=1.0, gt=0)


class MacroNewsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_file: str
    max_age_seconds: int = Field(gt=0)
    half_life_seconds: int = Field(gt=0)
    calculation_version: str = Field(min_length=1)


class DataPipelineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    normalization_version: str
    storage: StorageConfig
    fyers: FyersPipelineConfig
    macro_news: MacroNewsConfig
    session: SessionConfig
    quality: QualityConfig
    freshness: dict[str, dict[str, int]]
    underlyings: tuple[UnderlyingConfig, ...]


def load_data_pipeline_config(path: Path) -> DataPipelineConfig:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping")
    underlyings = tuple(
        UnderlyingConfig.model_validate(item) for item in raw.get("underlyings", [])
    )
    quality_raw = raw.get("quality", {})
    if not isinstance(quality_raw, dict):
        raise ValueError(f"{path}: quality must be a mapping")
    return DataPipelineConfig(
        schema_version=str(raw["schema_version"]),
        normalization_version=str(raw["normalization_version"]),
        storage=StorageConfig.model_validate(raw["storage"]),
        fyers=FyersPipelineConfig.model_validate(raw["fyers"]),
        macro_news=MacroNewsConfig.model_validate(raw["macro_news"]),
        session=SessionConfig.model_validate(raw["session"]),
        quality=QualityConfig.model_validate(quality_raw),
        freshness=raw.get("freshness", {}),
        underlyings=underlyings,
    )
