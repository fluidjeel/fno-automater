"""Versioned PAPER identification policy."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["AllowRule", "AllowTablePolicy", "IdentificationPolicy", "TimeWindow", "load_identification_policy"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WarmupPolicy(_Frozen):
    min_completed_bars: int = Field(ge=1)
    min_sessions: int = Field(ge=1)


class RegimePolicy(_Frozen):
    trend_threshold: Decimal = Field(gt=0, le=1)
    range_threshold: Decimal = Field(ge=0, le=1)
    volatility_expansion_ratio: Decimal = Field(gt=1)
    volatility_compression_ratio: Decimal = Field(gt=0, lt=1)


class ContractPolicy(_Frozen):
    min_open_interest: int = Field(ge=0)
    max_spread_fraction: Decimal = Field(gt=0, le=1)
    long_delta_min: Decimal = Field(ge=0, le=1)
    long_delta_max: Decimal = Field(ge=0, le=1)
    short_delta_min: Decimal = Field(ge=0, le=1)
    short_delta_max: Decimal = Field(ge=0, le=1)
    weekly_dte_min: int = Field(ge=0)
    weekly_dte_max: int = Field(ge=0)
    monthly_dte_min: int = Field(ge=0)
    monthly_dte_max: int = Field(ge=0)
    min_reward_risk: Decimal = Field(gt=0)
    estimated_round_trip_cost_per_lot: Decimal = Field(ge=0)


class RouterPolicy(_Frozen):
    low_iv_percentile: Decimal = Field(ge=0, le=100)
    low_iv_rv_ratio: Decimal = Field(gt=0)
    min_winner_score: Decimal = Field(ge=0, le=1)
    min_score_gap: Decimal = Field(ge=0, le=1)
    cooldown_minutes: int = Field(ge=0)



class TimeWindow(_Frozen):
    start: str
    end: str


class AllowRule(_Frozen):
    trend: tuple[str, ...]
    volatility: tuple[str, ...]
    iv_bucket: tuple[str, ...]
    event: tuple[str, ...]
    session: tuple[str, ...]
    allowed_families: tuple[str, ...]


class AllowTablePolicy(_Frozen):
    high_iv_percentile: Decimal = Field(ge=0, le=100)
    auction_windows_ist: tuple[TimeWindow, ...]
    continuous_window_ist: TimeWindow
    rules: tuple[AllowRule, ...]


class IdentificationPolicy(_Frozen):
    policy_version: str
    feature_version: str
    binding_version: str
    router_version: str
    vix_symbol: str
    warmup: WarmupPolicy
    regime: RegimePolicy
    contracts: ContractPolicy
    router: RouterPolicy
    allow_table: AllowTablePolicy


def load_identification_policy(path: Path) -> IdentificationPolicy:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("identification policy must be a mapping")
    payload.pop("schema_version", None)
    return IdentificationPolicy.model_validate(payload)
