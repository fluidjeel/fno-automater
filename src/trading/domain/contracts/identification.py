"""Typed contracts for deterministic Layer 3 market identification.

These records explain why a setup was selected or declined.  They carry no
quantity, broker command, or authority to bypass Layer 2.
"""

from __future__ import annotations

from enum import StrEnum, unique

from pydantic import Field

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import DataQuality, ReasonCode

__all__ = [
    "CandidateBinding",
    "ConfidenceKind",
    "MacroStatus",
    "MarketState",
    "RouteDecision",
    "SetupFeatures",
    "StructureKind",
    "TrendState",
    "VolatilityState",
]


@unique
class TrendState(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    RANGE = "RANGE"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


@unique
class VolatilityState(StrEnum):
    NORMAL = "NORMAL"
    EXPANDING = "EXPANDING"
    COMPRESSED = "COMPRESSED"
    UNKNOWN = "UNKNOWN"


@unique
class MacroStatus(StrEnum):
    ALIGNED = "ALIGNED"
    CONFLICT = "CONFLICT"
    NEUTRAL = "NEUTRAL"
    MISSING = "MISSING"
    STALE = "STALE"


@unique
class ConfidenceKind(StrEnum):
    RAW_SCORE = "RAW_SCORE"
    CALIBRATED_PROBABILITY = "CALIBRATED_PROBABILITY"


@unique
class StructureKind(StrEnum):
    LONG_OPTION = "LONG_OPTION"
    DEBIT_SPREAD = "DEBIT_SPREAD"
    CREDIT_SPREAD = "CREDIT_SPREAD"
    CAS_OPTION = "CAS_OPTION"
    COMMODITY_FUTURE = "COMMODITY_FUTURE"


class MarketState(VersionedModel):
    """Point-in-time regime state calculated only from completed inputs."""

    market_state_id: NonEmptyStr
    feature_version: NonEmptyStr
    calculated_at: UtcDatetime
    source_snapshot_ids: tuple[NonEmptyStr, ...]
    trend: TrendState
    volatility: VolatilityState
    return_15m: ExactDecimal | None = None
    return_60m: ExactDecimal | None = None
    normalized_return_15m: ExactDecimal | None = None
    normalized_return_60m: ExactDecimal | None = None
    normalized_vwap_distance: ExactDecimal | None = None
    realized_volatility_ratio: ExactDecimal | None = None
    realized_volatility_annualized: ExactDecimal | None = None
    iv_percentile: ExactDecimal | None = Field(default=None, ge=0, le=100)
    iv_rv_ratio: ExactDecimal | None = Field(default=None, ge=0)
    trend_score: ExactDecimal | None = Field(default=None, ge=-1, le=1)
    close_relative_move: ExactDecimal | None = None
    event_state: NonEmptyStr
    macro_status: MacroStatus
    quality: DataQuality
    warmup_complete: StrictBool
    completed_bar_count: StrictInt = Field(ge=0)
    session_count: StrictInt = Field(ge=0)
    reason_codes: tuple[ReasonCode, ...] = ()


class CandidateBinding(VersionedModel):
    """Selected contract symbols and the deterministic quality score."""

    strategy_id: NonEmptyStr
    binding_version: NonEmptyStr
    selected_symbols: tuple[NonEmptyStr, ...]
    score: ExactDecimal = Field(ge=0, le=1)
    eligible: StrictBool
    reason_codes: tuple[ReasonCode, ...] = ()
    rejected_symbols: tuple[NonEmptyStr, ...] = ()


class SetupFeatures(VersionedModel):
    """Frozen explanation attached to a newly emitted PAPER intent."""

    identification_rule_version: NonEmptyStr
    router_version: NonEmptyStr
    market_state_id: NonEmptyStr
    confidence_kind: ConfidenceKind = ConfidenceKind.RAW_SCORE
    raw_setup_score: ExactDecimal = Field(ge=0, le=1)
    score_components: dict[NonEmptyStr, ExactDecimal]
    trend: TrendState
    volatility: VolatilityState
    structure: StructureKind
    dte: StrictInt = Field(ge=0)
    delta: ExactDecimal | None = Field(default=None, ge=-1, le=1)
    implied_volatility: ExactDecimal | None = Field(default=None, ge=0)
    iv_percentile: ExactDecimal | None = Field(default=None, ge=0, le=100)
    iv_rv_ratio: ExactDecimal | None = Field(default=None, ge=0)
    open_interest: StrictInt | None = Field(default=None, ge=0)
    spread_fraction: ExactDecimal = Field(ge=0)
    liquidity_rank: ExactDecimal = Field(ge=0, le=1)
    event_state: NonEmptyStr
    macro_status: MacroStatus
    rejected_alternatives: tuple[NonEmptyStr, ...] = ()


class RouteDecision(VersionedModel):
    """One PAPER winner plus non-executing alternatives for evidence."""

    route_id: NonEmptyStr
    router_version: NonEmptyStr
    market_state_id: NonEmptyStr
    paper_winner: NonEmptyStr | None = None
    shadow_alternatives: tuple[NonEmptyStr, ...] = ()
    rejected_families: tuple[NonEmptyStr, ...] = ()
    reason_codes: tuple[ReasonCode, ...] = ()
    failed_gate_ids: tuple[NonEmptyStr, ...] = ()
    winner_score: ExactDecimal | None = Field(default=None, ge=0, le=1)
    score_gap: ExactDecimal | None = Field(default=None, ge=0, le=1)
