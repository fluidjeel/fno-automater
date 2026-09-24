"""Validated per-fill charge schedule loaded from config/charges.yaml."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from trading.config.loader import ConfigLoadError
from trading.config.risk_policy import MoneyAmount
from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictInt,
    VersionedModel,
)
from trading.domain.primitives import Currency, Money, Rounding

__all__ = [
    "ChargePolicyConfig",
    "ChargePolicyLoadError",
    "LoadedChargePolicy",
    "load_charge_policy",
]


class ChargePolicyLoadError(ConfigLoadError):
    """Raised when charge policy cannot be parsed or fails validation."""


class ChargePolicyConfig(VersionedModel):
    """Statutory rates for one fill's charge decomposition."""

    policy_version: NonEmptyStr
    default_contracts_per_lot: StrictInt = Field(gt=0)
    brokerage_per_order: MoneyAmount
    stt_sell_fraction: ExactDecimal = Field(ge=0)
    nse_transaction_fraction: ExactDecimal = Field(ge=0)
    clearing_fraction: ExactDecimal = Field(ge=0)
    sebi_turnover_fraction: ExactDecimal = Field(ge=0)
    ipft_options_fraction: ExactDecimal = Field(ge=0)
    stamp_duty_buy_fraction: ExactDecimal = Field(ge=0)
    gst_fraction: ExactDecimal = Field(ge=0, le=Decimal("1"))


@dataclass(frozen=True, slots=True)
class LoadedChargePolicy:
    """Charge policy with provenance for audit."""

    config: ChargePolicyConfig
    source: str
    checksum: str


def load_charge_policy(path: Path | None = None) -> LoadedChargePolicy:
    """Load charge policy from the repo config tree."""
    resolved = path or Path(__file__).resolve().parents[3] / "config" / "charges.yaml"
    raw = resolved.read_text(encoding="utf-8")
    return load_charge_policy_text(raw, source=str(resolved))


def load_charge_policy_text(raw: str, *, source: str) -> LoadedChargePolicy:
    """Parse charge policy YAML with checksum for lineage."""
    try:
        data: dict[str, Any] = yaml.safe_load(raw) or {}
        config = ChargePolicyConfig.model_validate(data)
    except Exception as exc:
        raise ChargePolicyLoadError(f"invalid charge policy at {source}: {exc}") from exc
    checksum = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return LoadedChargePolicy(config=config, source=source, checksum=checksum)


def round_trip_estimate_per_lot(
    policy: ChargePolicyConfig,
    *,
    charges_per_lot: Money,
) -> Money:
    """Model round-trip charge used until durable fills exist (CEILING)."""
    return charges_per_lot.quantized(Rounding.CEILING)
