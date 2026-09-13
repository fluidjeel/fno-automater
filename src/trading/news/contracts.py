"""Strict, versioned contracts for advisory news and macro evidence."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum, unique
from typing import Literal

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    StrictStr,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import DataQuality

__all__ = [
    "AssetImpact",
    "AssetImpactDirection",
    "EventRiskState",
    "EventRiskStatus",
    "MacroProposal",
    "MacroRegime",
    "NewsEvent",
    "NewsItem",
    "NewsQuality",
    "NewsSourceTier",
    "SentimentLabel",
    "SentimentProbabilities",
    "SentimentSnapshot",
]


@unique
class NewsSourceTier(StrEnum):
    OFFICIAL = "OFFICIAL"
    PRIMARY = "PRIMARY"
    AGGREGATOR = "AGGREGATOR"
    MEDIA = "MEDIA"
    SOCIAL = "SOCIAL"
    UNKNOWN = "UNKNOWN"


@unique
class NewsQuality(StrEnum):
    VALID = "VALID"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    INVALID = "INVALID"
    UNKNOWN = "UNKNOWN"


@unique
class SentimentLabel(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NEUTRAL = "NEUTRAL"
    UNKNOWN = "UNKNOWN"


@unique
class AssetImpactDirection(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"
    UNCERTAIN = "UNCERTAIN"


@unique
class EventRiskStatus(StrEnum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    BLOCK_NEW_ENTRIES = "BLOCK_NEW_ENTRIES"
    MARKET_EMERGENCY = "MARKET_EMERGENCY"


@unique
class MacroRegime(StrEnum):
    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"
    INFLATIONARY = "INFLATIONARY"
    DISINFLATIONARY = "DISINFLATIONARY"
    NEUTRAL = "NEUTRAL"
    ABSTAIN = "ABSTAIN"


class SentimentProbabilities(StrictModel):
    positive: ExactDecimal = Field(ge=0, le=1)
    negative: ExactDecimal = Field(ge=0, le=1)
    neutral: ExactDecimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _probabilities_sum_to_one(self) -> SentimentProbabilities:
        if self.positive + self.negative + self.neutral != Decimal(1):
            raise ValueError("sentiment probabilities must sum exactly to 1")
        return self


class NewsItem(VersionedModel):
    news_item_id: NonEmptyStr
    source_id: NonEmptyStr
    source_tier: NewsSourceTier
    canonical_url: NonEmptyStr
    url_hash: NonEmptyStr = Field(pattern=r"^[0-9a-f]{64}$")
    title_hash: NonEmptyStr = Field(pattern=r"^[0-9a-f]{64}$")
    headline: NonEmptyStr
    snippet: StrictStr = ""
    language: NonEmptyStr
    published_at: UtcDatetime
    retrieved_at: UtcDatetime
    content_hash: NonEmptyStr = Field(pattern=r"^[0-9a-f]{64}$")
    provider_metadata: dict[str, str | int | bool] = Field(default_factory=dict)
    quality_state: NewsQuality

    @model_validator(mode="after")
    def _publication_not_after_retrieval(self) -> NewsItem:
        if self.published_at > self.retrieved_at:
            raise ValueError("published_at cannot be after retrieved_at")
        return self


class NewsEvent(VersionedModel):
    event_id: NonEmptyStr
    event_type: NonEmptyStr
    entities: tuple[NonEmptyStr, ...] = ()
    instruments: tuple[NonEmptyStr, ...] = ()
    sectors: tuple[NonEmptyStr, ...] = ()
    assets: tuple[NonEmptyStr, ...] = ()
    first_seen_at: UtcDatetime
    latest_seen_at: UtcDatetime
    evidence_item_ids: tuple[NonEmptyStr, ...]
    original_source_id: NonEmptyStr
    duplicate_count: StrictInt = Field(ge=0)
    confirmation_count: StrictInt = Field(ge=0)
    contradictions: tuple[NonEmptyStr, ...] = ()
    novelty: ExactDecimal = Field(ge=0, le=1)
    freshness: ExactDecimal = Field(ge=0, le=1)
    expires_at: UtcDatetime
    quality_state: NewsQuality

    @model_validator(mode="after")
    def _event_times_and_evidence_are_consistent(self) -> NewsEvent:
        if self.latest_seen_at < self.first_seen_at:
            raise ValueError("latest_seen_at precedes first_seen_at")
        if self.expires_at <= self.latest_seen_at:
            raise ValueError("event expiry must follow latest_seen_at")
        if not self.evidence_item_ids:
            raise ValueError("a NewsEvent requires at least one evidence item")
        if self.confirmation_count > len(set(self.evidence_item_ids)):
            raise ValueError("confirmation_count cannot exceed distinct evidence")
        return self


class AssetImpact(VersionedModel):
    asset: NonEmptyStr
    direction: AssetImpactDirection
    magnitude: ExactDecimal = Field(ge=0, le=1)
    relevance: ExactDecimal = Field(ge=0, le=1)
    confidence: ExactDecimal = Field(ge=0, le=1)
    horizon: NonEmptyStr
    source_weight: ExactDecimal = Field(ge=0, le=1)
    sentiment_probabilities: SentimentProbabilities | None
    evidence_ids: tuple[NonEmptyStr, ...]
    calculation_version: NonEmptyStr
    expires_at: UtcDatetime

    @model_validator(mode="after")
    def _impact_has_evidence(self) -> AssetImpact:
        if not self.evidence_ids:
            raise ValueError("AssetImpact requires evidence IDs")
        return self


class SentimentSnapshot(VersionedModel):
    snapshot_id: NonEmptyStr
    as_of: UtcDatetime
    expires_at: UtcDatetime
    asset_impacts: tuple[AssetImpact, ...]
    event_cluster_ids: tuple[NonEmptyStr, ...]
    source_coverage: dict[NonEmptyStr, StrictInt]
    missing_source_ids: tuple[NonEmptyStr, ...]
    contradiction_indicators: tuple[NonEmptyStr, ...]
    model_version: NonEmptyStr
    config_version: NonEmptyStr
    config_checksum: NonEmptyStr
    quality_state: DataQuality
    lineage: tuple[NonEmptyStr, ...]

    @model_validator(mode="after")
    def _snapshot_is_time_bounded(self) -> SentimentSnapshot:
        if self.expires_at <= self.as_of:
            raise ValueError("snapshot expiry must follow as_of")
        return self


class EventRiskState(VersionedModel):
    scope: NonEmptyStr
    state: EventRiskStatus
    as_of: UtcDatetime
    expires_at: UtcDatetime
    event_ids: tuple[NonEmptyStr, ...]
    reason_codes: tuple[NonEmptyStr, ...]
    quality_state: NewsQuality

    @model_validator(mode="after")
    def _risk_state_is_expiring(self) -> EventRiskState:
        if self.expires_at <= self.as_of:
            raise ValueError("event-risk state must expire after as_of")
        return self


class ProposalEvidence(StrictModel):
    evidence_id: NonEmptyStr
    source_id: NonEmptyStr
    retrieved_at: UtcDatetime
    canonical_url: NonEmptyStr


class MacroProposal(VersionedModel):
    proposal_id: NonEmptyStr
    as_of: UtcDatetime
    valid_until: UtcDatetime
    regime: MacroRegime
    allowed_strategy_families: tuple[NonEmptyStr, ...]
    blocked_strategy_families: tuple[NonEmptyStr, ...]
    evidence: tuple[ProposalEvidence, ...]
    contradictions: tuple[NonEmptyStr, ...]
    assumptions: tuple[NonEmptyStr, ...]
    abstain: StrictBool
    rationale: StrictStr
    model_version: NonEmptyStr
    prompt_version: NonEmptyStr
    retrieval_version: NonEmptyStr
    policy_version: NonEmptyStr

    @model_validator(mode="after")
    def _proposal_is_grounded_and_bounded(self) -> MacroProposal:
        if self.valid_until <= self.as_of:
            raise ValueError("proposal validity must end after as_of")
        if self.abstain and self.regime is not MacroRegime.ABSTAIN:
            raise ValueError("abstaining proposal must use ABSTAIN regime")
        if not self.abstain and not self.evidence:
            raise ValueError("non-abstaining proposal requires grounded evidence")
        if set(self.allowed_strategy_families) & set(self.blocked_strategy_families):
            raise ValueError("strategy family cannot be both allowed and blocked")
        return self


MacroNewsInput = Literal["RSS", "GDELT", "FRED", "EIA", "GOOGLE_NEWS_RSS"]
