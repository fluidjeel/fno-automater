"""Validated Layer 2 risk policy loaded from config/risk.yaml."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, unique
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, model_validator

from trading.config.loader import ConfigLoadError
from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    VersionedModel,
)
from trading.domain.primitives import Currency, Money

__all__ = [
    "LoadedRiskPolicy",
    "MissingMonitorResolution",
    "RiskPolicyConfig",
    "RiskPolicyLoadError",
    "StrategyAllocation",
    "load_risk_policy",
]


class RiskPolicyLoadError(ConfigLoadError):
    """Raised when risk policy cannot be parsed or fails validation."""


@unique
class MissingMonitorResolution(StrEnum):
    """Deterministic action after an open position loses its monitor quote."""

    HALT_ONLY = "HALT_ONLY"
    FLATTEN_REMAINING = "FLATTEN_REMAINING"


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
    # ADESK-A4 portfolio exposure caps (PART 6).
    net_vega_limit: ExactDecimal = Field(gt=0)
    expiry_day_notional_fraction: ExactDecimal = Field(gt=0, le=Decimal("1"))
    directional_agreement_max: ExactDecimal = Field(gt=0, le=Decimal("1"))
    single_event_exposure_fraction: ExactDecimal = Field(gt=0, le=Decimal("1"))
    # ADESK-A5 PART 8 — max |worst_case|/equity before entry freeze.
    tail_budget_fraction: ExactDecimal = Field(gt=0, le=Decimal("1"))
    # Portfolio arbitration cap on concurrent M4 open/pending positions.
    max_m4_open_positions: StrictInt = Field(default=2, ge=1)
    # Portfolio open-risk ceiling across all modes (absolute money).
    max_global_open_risk: MoneyAmount | None = None
    decision_ttl_seconds: StrictInt = Field(gt=0)
    # Defaults to False so a policy that omits the key refuses stop-bounded
    # futures shorts: the fail-closed direction for a new risk class.
    allow_stop_bounded_futures_short: StrictBool = False
    # PAPER SPAN substitute. Null fails closed for live-symbol futures sizing.
    paper_future_margin_fraction: ExactDecimal | None = Field(
        default=None, gt=0, le=Decimal("1")
    )
    missing_monitor_resolution: MissingMonitorResolution = (
        MissingMonitorResolution.HALT_ONLY
    )

    @model_validator(mode="after")
    def _allocations_fit_equity(self) -> RiskPolicyConfig:
        allocated = sum(
            (
                allocation.allocation_fraction
                for allocation in self.strategy_allocations.values()
            ),
            Decimal(0),
        )
        if allocated > Decimal(1):
            raise ValueError(
                f"strategy allocations total {allocated}; aggregate allocation "
                "cannot exceed account equity"
            )
        return self

    def has_allocation(self, strategy_id: str) -> bool:
        """Whether policy scopes capital to this strategy.

        Checked before sizing so an unlisted strategy is refused with a reason
        code rather than raising out of the decision path.
        """
        return strategy_id in self.strategy_allocations

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
