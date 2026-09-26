"""Discovery mode configuration models and loader (DISC-A0 / DISCOVERY_MODE.md)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Self

import yaml
from pydantic import Field, model_validator

from trading.config.loader import ConfigLoadError
from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictInt,
    StrictModel,
    VersionedModel,
)
from trading.domain.enums import ModeId, ReasonCode

__all__ = [
    "DeltaBand",
    "DiscoveryBooksConfig",
    "DiscoveryConfig",
    "DiscoveryConfigError",
    "DiscoveryDirectionConfig",
    "DiscoveryFillsConfig",
    "DiscoveryModeConfig",
    "DiscoverySelectionConfig",
    "DteBand",
    "MaxSpreadFractionConfig",
    "MinOpenInterestConfig",
    "load_discovery_config",
]


class DiscoveryConfigError(ConfigLoadError):
    """Raised when discovery configuration cannot be loaded or validated."""


class DiscoveryBooksConfig(StrictModel):
    """Books and compounding settings for independent mode accounts."""

    starting_equity_per_mode: ExactDecimal = Field(gt=Decimal("0"))
    compounding: NonEmptyStr = "DAILY_REALIZED_NET"
    drawdown_alert_fraction: ExactDecimal = Field(gt=Decimal("0"), le=Decimal("1"))


class DiscoveryModeConfig(StrictModel):
    """Risk and position constraints for a single mode."""

    per_trade_guide: ExactDecimal = Field(gt=Decimal("0"), le=Decimal("1"))
    open_risk_cap: ExactDecimal = Field(gt=Decimal("0"), le=Decimal("1"))
    max_new_entries_per_day: StrictInt = Field(ge=0)
    max_open_positions: StrictInt = Field(ge=0)


class DiscoveryFillsConfig(StrictModel):
    """Execution fill models for discovery and strict shadow evaluation."""

    model: NonEmptyStr = "touch-v1"
    shadow_model: NonEmptyStr = "conservative-v1"


class DeltaBand(StrictModel):
    """Strict and fallback delta ranges for option contract selection."""

    strict: tuple[ExactDecimal, ExactDecimal]
    fallback: tuple[ExactDecimal, ExactDecimal]

    @model_validator(mode="after")
    def validate_bands(self) -> Self:
        for band in (self.strict, self.fallback):
            if band[0] > band[1]:
                raise ValueError(f"delta band min {band[0]} > max {band[1]}")
            for v in band:
                if v < Decimal("0") or v > Decimal("1"):
                    raise ValueError(f"delta {v} must be between 0 and 1")
        return self


class DteBand(StrictModel):
    """Strict and fallback days-to-expiry ranges."""

    strict: tuple[StrictInt, StrictInt]
    fallback: tuple[StrictInt, StrictInt]

    @model_validator(mode="after")
    def validate_bands(self) -> Self:
        for band in (self.strict, self.fallback):
            if band[0] > band[1]:
                raise ValueError(f"dte band min {band[0]} > max {band[1]}")
            for v in band:
                if v < 0:
                    raise ValueError(f"dte {v} cannot be negative")
        return self


class MinOpenInterestConfig(StrictModel):
    """Minimum open interest thresholds."""

    strict: StrictInt = Field(ge=0)
    fallback: StrictInt = Field(ge=0)


class MaxSpreadFractionConfig(StrictModel):
    """Maximum bid-ask spread fraction thresholds."""

    strict: ExactDecimal = Field(gt=Decimal("0"), le=Decimal("1"))
    fallback: ExactDecimal = Field(gt=Decimal("0"), le=Decimal("1"))


class DiscoverySelectionConfig(StrictModel):
    """Contract selection parameters for discovery mode."""

    m2_delta: DeltaBand
    m1_delta: DeltaBand
    long_delta: DeltaBand
    short_delta: DeltaBand
    min_open_interest: MinOpenInterestConfig
    max_spread_fraction: MaxSpreadFractionConfig
    weekly_dte: DteBand
    near_strikes_each_side: StrictInt = Field(ge=0)
    following_week_strikes_each_side: StrictInt = Field(ge=0)


class DiscoveryDirectionConfig(StrictModel):
    """Fallback directional parameters when strict trend is neutral."""

    fallback_trend_threshold: ExactDecimal = Field(gt=Decimal("0"), le=Decimal("1"))
    fallback_min_bars: StrictInt = Field(ge=1)


class DiscoveryConfig(VersionedModel):
    """Validated discovery profile configuration (config/discovery.yaml)."""

    profile_version: NonEmptyStr
    books: DiscoveryBooksConfig
    modes: dict[str, DiscoveryModeConfig]
    bug_guard_trade_risk_fraction: ExactDecimal = Field(
        gt=Decimal("0"), le=Decimal("1")
    )
    hard_quote_max_age_ms: StrictInt = Field(gt=0)
    min_dte_new_entry: dict[str, StrictInt]
    fills: DiscoveryFillsConfig
    soft_reason_codes: tuple[ReasonCode, ...]
    selection: DiscoverySelectionConfig
    direction: DiscoveryDirectionConfig

    @model_validator(mode="after")
    def validate_required_modes(self) -> Self:
        required = {
            ModeId.M1_CAS.value,
            ModeId.M2_DIRECTIONAL.value,
            ModeId.M3_TACTICAL_POSITIONAL.value,
            ModeId.M4_STRATEGIC_POSITIONAL.value,
        }
        missing = required - set(self.modes.keys())
        if missing:
            raise ValueError(
                f"missing required modes in discovery config: {sorted(missing)}"
            )
        return self


def load_discovery_config(path: Path) -> DiscoveryConfig:
    """Load and validate discovery.yaml from the filesystem."""
    if not path.is_file():
        raise DiscoveryConfigError(f"discovery config file not found: {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise DiscoveryConfigError(f"failed to read yaml from {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DiscoveryConfigError(f"discovery config in {path} must be a mapping")
    try:
        return DiscoveryConfig.model_validate(payload)
    except Exception as exc:
        raise DiscoveryConfigError(
            f"failed to validate discovery config from {path}: {exc}"
        ) from exc
