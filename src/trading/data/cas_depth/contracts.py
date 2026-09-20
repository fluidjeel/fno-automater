"""CAS depth-only contracts. No broker or order semantics."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictInt,
    StrictModel,
    UtcDatetime,
)

__all__ = [
    "CAS_DEPTH_FEATURE_SET_VERSION",
    "CasDepthFeatureSnapshot",
    "DepthLevel",
    "DepthQualityIssue",
    "NormalizedDepthUpdate",
    "TradeAggressor",
]

CAS_DEPTH_FEATURE_SET_VERSION = "cas-depth-only-v1"

FEATURE_TOP5_IMBALANCE = "cas_depth_top5_imbalance"
FEATURE_TOP50_IMBALANCE = "cas_depth_top50_imbalance"
FEATURE_MICROPRICE = "cas_depth_microprice"
FEATURE_SPREAD = "cas_depth_spread"
FEATURE_DEPTH_SLOPE = "cas_depth_slope"
FEATURE_LIQUIDITY_GAP = "cas_depth_liquidity_gap"
FEATURE_QUOTE_STABILITY = "cas_depth_quote_stability"
FEATURE_REPLENISHMENT = "cas_depth_replenishment"

CAS_DEPTH_FEATURE_KEYS = (
    FEATURE_TOP5_IMBALANCE,
    FEATURE_TOP50_IMBALANCE,
    FEATURE_MICROPRICE,
    FEATURE_SPREAD,
    FEATURE_DEPTH_SLOPE,
    FEATURE_LIQUIDITY_GAP,
    FEATURE_QUOTE_STABILITY,
    FEATURE_REPLENISHMENT,
)


class TradeAggressor(StrEnum):
    """Trade initiator side. UNKNOWN unless a reliable trade tape exists."""

    UNKNOWN = "UNKNOWN"
    BUY = "BUY"
    SELL = "SELL"


class DepthQualityIssue(StrEnum):
    """Recorded quality anomalies for CAS depth updates."""

    STALE = "STALE"
    DUPLICATE = "DUPLICATE"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    MALFORMED = "MALFORMED"
    MISSING_FIELD = "MISSING_FIELD"
    RECONNECT = "RECONNECT"
    QUEUE_OVERFLOW = "QUEUE_OVERFLOW"


class DepthLevel(StrictModel):
    """One price level in a normalized depth book."""

    price: ExactDecimal
    quantity: StrictInt = Field(ge=0)
    orders: StrictInt | None = Field(default=None, ge=0)


class NormalizedDepthUpdate(StrictModel):
    """Provider-agnostic depth update. Levels are price-sorted."""

    symbol: NonEmptyStr
    exchange_timestamp: UtcDatetime
    receive_timestamp: UtcDatetime
    sequence: StrictInt | None = None
    bid_levels: tuple[DepthLevel, ...]
    ask_levels: tuple[DepthLevel, ...]
    ltp: ExactDecimal | None = None
    last_quantity: StrictInt | None = Field(default=None, ge=0)
    total_volume: StrictInt | None = Field(default=None, ge=0)
    open_interest: StrictInt | None = Field(default=None, ge=0)
    trade_aggressor: TradeAggressor = TradeAggressor.UNKNOWN
    is_snapshot: bool = False
    feed: NonEmptyStr
    quality_issues: tuple[DepthQualityIssue, ...] = ()


class CasDepthFeatureSnapshot(StrictModel):
    """Bounded depth-only feature snapshot for CAS paper consumption."""

    snapshot_id: NonEmptyStr
    symbol: NonEmptyStr
    feature_set_version: NonEmptyStr = CAS_DEPTH_FEATURE_SET_VERSION
    exchange_timestamp: UtcDatetime
    receive_timestamp: UtcDatetime
    calculation_timestamp: UtcDatetime
    features: dict[NonEmptyStr, ExactDecimal]
    trade_aggressor: TradeAggressor = TradeAggressor.UNKNOWN
    quality_issues: tuple[DepthQualityIssue, ...] = ()
    abstain: bool = False
    abstain_reason: str | None = None

    @property
    def is_valid(self) -> bool:
        """True when every declared depth feature is present and abstain is false."""
        if self.abstain:
            return False
        return all(key in self.features for key in CAS_DEPTH_FEATURE_KEYS)
