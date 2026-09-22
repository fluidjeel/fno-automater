from __future__ import annotations

from decimal import Decimal

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictInt,
    StrictModel,
)
from trading.universe.contracts import ConvictionLevel


class UniverseScannerConfig(StrictModel):
    scan_version: NonEmptyStr
    scan_interval_minutes: StrictInt = Field(default=5, ge=1, le=60)
    top_candidates: StrictInt = Field(default=5, ge=1, le=20)
    min_avg_daily_volume: StrictInt = Field(default=500000, ge=0)
    min_option_oi: StrictInt = Field(default=1000, ge=0)
    max_spread_fraction: ExactDecimal = Field(default=Decimal("0.05"), ge=0)

    # Scoring weights (must sum to 1.0)
    weight_relative_strength: ExactDecimal = Field(default=Decimal("0.25"))
    weight_sector_rs: ExactDecimal = Field(default=Decimal("0.15"))
    weight_volume_surge: ExactDecimal = Field(default=Decimal("0.15"))
    weight_vwap_distance: ExactDecimal = Field(default=Decimal("0.15"))
    weight_oi_buildup: ExactDecimal = Field(default=Decimal("0.15"))
    weight_price_structure: ExactDecimal = Field(default=Decimal("0.15"))

    # Conviction thresholds
    conviction_none_max: ExactDecimal = Field(default=Decimal("50"))
    conviction_weak_max: ExactDecimal = Field(default=Decimal("65"))
    conviction_moderate_max: ExactDecimal = Field(default=Decimal("75"))
    conviction_strong_max: ExactDecimal = Field(default=Decimal("85"))

    # Entry requires at least this conviction level
    min_entry_conviction: ConvictionLevel = Field(default=ConvictionLevel.MODERATE)
    min_entry_conviction_regime_aligned: ConvictionLevel = Field(
        default=ConvictionLevel.MODERATE
    )
    min_entry_conviction_regime_opposed: ConvictionLevel = Field(
        default=ConvictionLevel.STRONG
    )

    @model_validator(mode="after")
    def validate_weights(self) -> UniverseScannerConfig:
        total = (
            self.weight_relative_strength
            + self.weight_sector_rs
            + self.weight_volume_surge
            + self.weight_vwap_distance
            + self.weight_oi_buildup
            + self.weight_price_structure
        )
        if total != Decimal("1.00"):
            raise ValueError(f"Scoring weights must sum to 1.00, got {total}")
        return self

__all__ = ["UniverseScannerConfig"]
