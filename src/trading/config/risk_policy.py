"""Validated Layer 2 risk policy loaded from config/risk.yaml."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from trading.config.loader import ConfigLoadError
from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictInt,
    StrictModel,
    VersionedModel,
)
from trading.domain.primitives import Currency, Money

__all__ = [
    "LoadedRiskPolicy",
    "RiskPolicyConfig",
    "RiskPolicyLoadError",
    "StrategyAllocation",
    "load_risk_policy",
]


class RiskPolicyLoadError(ConfigLoadError):
    """Raised when risk policy cannot be parsed or fails validation."""


class MoneyAmount(StrictModel):
    """Money field for YAML configuration."""

    amount: ExactDecimal = Field(gt=Decimal(0))
    currency: Currency

    def to_money(self) -> Money:
        return Money.of(self.amount, self.currency)


class StrategyAllocation(StrictModel):
    """Capital budget fraction for one strategy."""

    allocation_fraction: ExactDecimal = Field(gt=0, le=Decimal("1"))


class RiskPolicyConfig(VersionedModel):
    """Strategy-scoped limits and sizing buffers."""

    policy_version: NonEmptyStr
    strategy_allocations: dict[NonEmptyStr, StrategyAllocation]
    underlying_concentration_fraction: ExactDecimal = Field(gt=0, le=Decimal("1"))
    options_premium_budget_fraction: ExactDecimal = Field(gt=0, le=Decimal("1"))
    slippage_buffer_fraction: ExactDecimal = Field(ge=0, le=Decimal("0.1"))
    charges_per_lot: MoneyAmount
    net_delta_limit: StrictInt = Field(gt=0)
    decision_ttl_seconds: StrictInt = Field(gt=0)

    def allocation_for(self, strategy_id: str) -> StrategyAllocation:
        if strategy_id not in self.strategy_allocations:
            raise RiskPolicyLoadError(
                f"strategy {strategy_id} has no allocation in risk policy"
            )
        return self.strategy_allocations[strategy_id]


@dataclass(frozen=True, slots=True)
class LoadedRiskPolicy:
    """Risk policy plus the identity of the bytes it came from."""

    config: RiskPolicyConfig
    version: str
    checksum: str
    source: str

    @property
    def lineage(self) -> tuple[str, str]:
        return self.config.policy_version, self.checksum


def _checksum(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_risk_policy_text(
    raw: str,
    *,
    source: str = "<string>",
) -> LoadedRiskPolicy:
    """Parse, validate and checksum risk policy from text."""
    try:
        parsed: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RiskPolicyLoadError(f"{source}: not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RiskPolicyLoadError(
            f"{source}: expected a mapping at the top level, got "
            f"{type(parsed).__name__}"
        )
    config = RiskPolicyConfig.model_validate(parsed)
    return LoadedRiskPolicy(
        config=config,
        version=config.schema_version,
        checksum=_checksum(raw.encode("utf-8")),
        source=source,
    )


def load_risk_policy(path: Path) -> LoadedRiskPolicy:
    """Load risk policy from a file."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RiskPolicyLoadError(f"cannot read {path}: {exc}") from exc
    return load_risk_policy_text(raw, source=str(path))
