"""Versioned per-mode forecast configuration (``config/forecast.yaml``)."""

from __future__ import annotations

import hashlib
from datetime import time
from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = [
    "EntryWindow",
    "ForecastConfig",
    "LogisticModelConfig",
    "M1ForecastConfig",
    "M2ForecastConfig",
    "M3ForecastConfig",
    "M4ForecastConfig",
    "ModeForecastConfig",
    "load_forecast_config",
]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LogisticModelConfig(_Frozen):
    """Frozen coefficients; refit offline, never in the live path."""

    intercept: Decimal = Decimal(0)
    coefficients: dict[str, Decimal]


class EntryWindow(_Frozen):
    """Exchange-local half-open window ``[start, end)``."""

    start: str
    end: str

    def contains(self, local: time) -> bool:
        return _parse(self.start) <= local < _parse(self.end)


def _parse(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


class ModeForecastConfig(_Frozen):
    """Fields every mode's forecaster shares."""

    enforce: bool = False
    enforce_exits: bool = False
    horizon_minutes: int = Field(gt=0)
    record_interval_seconds: int = Field(ge=0)
    min_directional_margin: Decimal = Field(ge=0, lt=Decimal("0.5"))
    event_threshold_sigma: Decimal = Field(gt=0)
    target_sigma: Decimal = Field(gt=0)
    stop_sigma: Decimal = Field(gt=0)
    min_edge_fraction: Decimal = Field(ge=0)
    entry_windows_ist: tuple[EntryWindow, ...] = ()
    model: LogisticModelConfig


class M1ForecastConfig(ModeForecastConfig):
    ofi_short_seconds: int = Field(gt=0)
    ofi_long_seconds: int = Field(gt=0)
    cost_hurdle_multiple: Decimal = Field(gt=0)
    decay_exit_probability: Decimal = Field(gt=0, lt=1)
    blocked_time_buckets: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _windows_ordered(self) -> M1ForecastConfig:
        if self.ofi_short_seconds >= self.ofi_long_seconds:
            raise ValueError("ofi_short_seconds must be shorter than ofi_long_seconds")
        return self


class M2ForecastConfig(ModeForecastConfig):
    opening_range_minutes: int = Field(gt=0)
    breakout_min_range_atr: Decimal = Field(ge=0)
    invalidation_buffer_atr: Decimal = Field(ge=0)
    daily_trend_sma: int = Field(ge=2)


class M3ForecastConfig(ModeForecastConfig):
    swing_lookback_bars: int = Field(ge=5)
    daily_trend_sma: int = Field(ge=2)
    debit_max_iv_percentile: Decimal = Field(ge=0, le=100)
    credit_min_iv_percentile: Decimal = Field(ge=0, le=100)
    credit_short_quantile: Decimal = Field(gt=Decimal("0.5"), lt=1)
    debit_target_quantile: Decimal = Field(gt=Decimal("0.5"), lt=1)
    credit_width_points: Decimal = Field(gt=0)
    invalidation_buffer_atr: Decimal = Field(ge=0)

    @model_validator(mode="after")
    def _iv_bands_ordered(self) -> M3ForecastConfig:
        if self.debit_max_iv_percentile > self.credit_min_iv_percentile:
            raise ValueError("debit IV ceiling must not exceed credit IV floor")
        return self


class HarConfig(_Frozen):
    intercept: Decimal = Decimal(0)
    daily: Decimal = Field(ge=0)
    weekly: Decimal = Field(ge=0)
    monthly: Decimal = Field(ge=0)


class M4ForecastConfig(ModeForecastConfig):
    har: HarConfig
    fast_sma: int = Field(ge=2)
    slow_sma: int = Field(ge=2)
    short_vol_min_ratio: Decimal = Field(gt=0)
    long_vol_max_ratio: Decimal = Field(gt=0)
    range_margin: Decimal = Field(ge=0, lt=Decimal("0.5"))
    directional_margin: Decimal = Field(ge=0, lt=Decimal("0.5"))
    event_min_importance: int = Field(ge=0)
    event_cheap_ratio: Decimal = Field(gt=0)
    shock_gap_sigma: Decimal = Field(gt=0)
    shock_vix_change: Decimal = Field(gt=0)
    max_net_delta_units: Decimal = Field(gt=0)
    short_strike_buffer_fraction: Decimal = Field(ge=0, lt=Decimal("0.1"))

    @model_validator(mode="after")
    def _vol_bands_ordered(self) -> M4ForecastConfig:
        if self.long_vol_max_ratio >= self.short_vol_min_ratio:
            raise ValueError("long-vol ceiling must sit below the short-vol floor")
        if self.fast_sma >= self.slow_sma:
            raise ValueError("fast_sma must be shorter than slow_sma")
        return self


class ForecastConfig(_Frozen):
    model_version: str
    annual_trading_days: int = Field(gt=0)
    session_minutes: int = Field(gt=0)
    bar_seconds: int = Field(gt=0)
    m1: M1ForecastConfig
    m2: M2ForecastConfig
    m3: M3ForecastConfig
    m4: M4ForecastConfig


def load_forecast_config(path: Path) -> tuple[ForecastConfig, str]:
    """Return the validated config and a byte-checksummed version string."""
    raw = path.read_bytes()
    payload = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("forecast config must be a mapping")
    payload.pop("schema_version", None)
    config = ForecastConfig.model_validate(payload)
    version = f"{config.model_version}+{hashlib.sha256(raw).hexdigest()[:12]}"
    return config, version
