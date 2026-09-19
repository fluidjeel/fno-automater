"""Layer 4 evaluation policy: fill-model rules and frozen eligibility gates."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from trading.config.loader import ConfigLoadError
from trading.config.schema import Environment, VerifiedValue
from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    VersionedModel,
)
from trading.domain.enums import ExecutionMode
from trading.domain.primitives import Currency, Money

__all__ = [
    "EligibilityThresholds",
    "EvaluationConfig",
    "EvaluationConfigError",
    "FillModelConfig",
    "LoadedEvaluationConfig",
    "assert_execution_mode_allowed",
    "load_evaluation_config",
]


class EvaluationConfigError(ConfigLoadError):
    """Raised when evaluation policy cannot be parsed or fails validation."""


class FillModelConfig(StrictModel):
    """Conservative fill reconstruction parameters."""

    version: NonEmptyStr
    slippage_ticks: StrictInt = Field(ge=0)
    legging_delay_ticks: StrictInt = Field(ge=0)
    require_traded_through: StrictBool = True
    charges_per_lot: VerifiedValue[Decimal]


class EligibilityThresholds(StrictModel):
    """Promotion gates frozen before a cohort is scored."""

    min_observation_days: StrictInt = Field(ge=0)
    min_signals: StrictInt = Field(ge=0)
    min_closed_trades: StrictInt = Field(ge=0)
    min_expectancy_amount: ExactDecimal
    max_drawdown_amount: ExactDecimal = Field(ge=0)
    max_consecutive_losses: StrictInt = Field(ge=0)
    min_fill_rate: ExactDecimal = Field(ge=0, le=1)
    max_reject_rate: ExactDecimal = Field(ge=0, le=1)
    max_average_slippage_amount: ExactDecimal = Field(ge=0)
    max_single_trade_pnl_share: ExactDecimal = Field(ge=0, le=1)
    min_regime_count: StrictInt = Field(ge=0)
    require_daily_reconciliation: StrictBool = True
    require_no_p0_p1: StrictBool = True
    require_confirmed_costs: StrictBool = True
    min_profit_factor: ExactDecimal = Field(default=Decimal(0), ge=0)
    require_positive_expectancy_lower_95: StrictBool = False
    currency: Currency

    def min_expectancy(self) -> Money:
        return Money.of(self.min_expectancy_amount, self.currency)

    def max_drawdown(self) -> Money:
        return Money.of(self.max_drawdown_amount, self.currency)

    def max_average_slippage(self) -> Money:
        return Money.of(self.max_average_slippage_amount, self.currency)


class EvaluationConfig(VersionedModel):
    """Versioned Layer 4 evaluation policy."""

    policy_version: NonEmptyStr
    fill_model: FillModelConfig
    eligibility: EligibilityThresholds


@dataclass(frozen=True, slots=True)
class LoadedEvaluationConfig:
    """Evaluation policy plus the identity of the bytes it came from."""

    config: EvaluationConfig
    version: str
    checksum: str
    source: str


def assert_execution_mode_allowed(
    environment: Environment,
    mode: ExecutionMode,
) -> None:
    """Refuse real-capital modes unless the process is LIVE."""
    if mode.touches_real_capital and environment is not Environment.LIVE:
        raise EvaluationConfigError(
            f"{mode} touches real capital but process environment is "
            f"{environment}; only LIVE may run CANARY_REAL or later"
        )


def _checksum(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_evaluation_config_text(
    raw: str,
    *,
    source: str = "<string>",
) -> LoadedEvaluationConfig:
    """Parse, validate and checksum evaluation policy from text."""
    try:
        parsed: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise EvaluationConfigError(f"{source}: not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise EvaluationConfigError(
            f"{source}: expected a mapping at the top level, got "
            f"{type(parsed).__name__}"
        )
    config = EvaluationConfig.model_validate(parsed)
    return LoadedEvaluationConfig(
        config=config,
        version=config.schema_version,
        checksum=_checksum(raw.encode("utf-8")),
        source=source,
    )


def load_evaluation_config(path: Path) -> LoadedEvaluationConfig:
    """Load evaluation policy from a file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvaluationConfigError(f"cannot read {path}: {exc}") from exc
    return load_evaluation_config_text(raw, source=str(path))
