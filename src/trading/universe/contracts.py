from __future__ import annotations

from enum import StrEnum, unique
from typing import ClassVar

from pydantic import Field

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    UtcDatetime,
    VersionedModel,
)


@unique
class MarketRegime(StrEnum):
    STRONG_BULL = "STRONG_BULL"
    MILD_BULL = "MILD_BULL"
    NEUTRAL = "NEUTRAL"
    MILD_BEAR = "MILD_BEAR"
    STRONG_BEAR = "STRONG_BEAR"


@unique
class TradeDirection(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


@unique
class ConvictionLevel(StrEnum):
    NONE = "NONE"
    WEAK = "WEAK"
    MODERATE = "MODERATE"
    STRONG = "STRONG"
    VERY_STRONG = "VERY_STRONG"


@unique
class CarryForwardAction(StrEnum):
    CLOSE = "CLOSE"
    CARRY_FORWARD = "CARRY_FORWARD"


class StockScore(VersionedModel):
    __version__: ClassVar[str] = "1.0.0"
    
    symbol: NonEmptyStr
    underlying: NonEmptyStr
    direction: TradeDirection
    composite_score: ExactDecimal = Field(ge=0, le=100)
    relative_strength: ExactDecimal
    sector_rs: ExactDecimal | None
    volume_surge_ratio: ExactDecimal
    vwap_distance: ExactDecimal
    oi_buildup_score: ExactDecimal
    price_structure_score: ExactDecimal
    sector: NonEmptyStr
    calculated_at: UtcDatetime


class UniverseScanResult(VersionedModel):
    __version__: ClassVar[str] = "1.0.0"
    
    scan_id: NonEmptyStr
    scanned_at: UtcDatetime
    regime: MarketRegime
    total_scanned: StrictInt
    long_candidates: tuple[StockScore, ...]
    short_candidates: tuple[StockScore, ...]
    scan_version: NonEmptyStr


class ConvictionAssessment(VersionedModel):
    __version__: ClassVar[str] = "1.0.0"
    
    assessment_id: NonEmptyStr
    symbol: NonEmptyStr
    direction: TradeDirection
    conviction_level: ConvictionLevel
    conviction_score: ExactDecimal = Field(ge=0, le=100)
    trend_alignment_score: ExactDecimal = Field(ge=0, le=100)
    volume_confirmation_score: ExactDecimal = Field(ge=0, le=100)
    oi_confirmation_score: ExactDecimal = Field(ge=0, le=100)
    price_action_score: ExactDecimal = Field(ge=0, le=100)
    sector_support_score: ExactDecimal = Field(ge=0, le=100)
    regime_alignment_score: ExactDecimal = Field(ge=0, le=100)
    assessed_at: UtcDatetime


class CarryForwardDecision(VersionedModel):
    __version__: ClassVar[str] = "1.0.0"
    
    decision_id: NonEmptyStr
    symbol: NonEmptyStr
    action: CarryForwardAction
    current_pnl: ExactDecimal
    conviction_at_close: ConvictionAssessment
    regime_aligned: StrictBool
    in_profit: StrictBool
    decided_at: UtcDatetime
    reason: NonEmptyStr

__all__ = [
    "MarketRegime",
    "TradeDirection",
    "ConvictionLevel",
    "CarryForwardAction",
    "StockScore",
    "UniverseScanResult",
    "ConvictionAssessment",
    "CarryForwardDecision",
]
