"""Validated, versioned source and scoring configuration for news processing."""

from __future__ import annotations

from datetime import time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trading.domain.contracts.base import ExactDecimal, NonEmptyStr
from trading.news.contracts import NewsSourceTier

__all__ = [
    "NewsSourceConfig",
    "NewsSubsystemConfig",
    "NewsTaxonomyRule",
    "load_news_config",
]

SourceKind = Literal["GDELT", "RSS", "FRED", "EIA", "GOOGLE_NEWS_RSS"]
SentimentProvider = Literal["unavailable", "finbert"]


class NewsSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_id: NonEmptyStr
    kind: SourceKind
    tier: NewsSourceTier
    endpoint: str = ""
    enabled: bool = False
    weight: ExactDecimal = Field(ge=0, le=1)
    timeout_seconds: int = Field(gt=0, le=60)
    max_items: int = Field(gt=0, le=1000)
    interval_seconds: int = Field(gt=0)
    api_key_env: str = ""
    query: str = ""
    params: dict[str, str] = Field(default_factory=dict)
    language: str = "en"
    assume_timezone: str = ""
    retry_attempts: int = Field(default=3, ge=1, le=5)
    circuit_failure_threshold: int = Field(default=3, ge=1)
    circuit_cooldown_seconds: int = Field(default=120, gt=0)
    max_payload_bytes: int = Field(default=2_000_000, gt=0)

    @field_validator("endpoint")
    @classmethod
    def _secure_endpoint(cls, value: str) -> str:
        if value and not value.startswith("https://"):
            raise ValueError("news source endpoints must use HTTPS")
        return value

    @field_validator("assume_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        if value:
            try:
                ZoneInfo(value)
            except ZoneInfoNotFoundError as exc:
                raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _enabled_source_has_endpoint(self) -> NewsSourceConfig:
        if self.enabled and not self.endpoint:
            raise ValueError("enabled source requires endpoint")
        if self.kind in {"FRED", "EIA"} and self.enabled and not self.api_key_env:
            raise ValueError("enabled FRED/EIA source requires api_key_env")
        return self


class NewsTaxonomyRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: NonEmptyStr
    keywords: tuple[NonEmptyStr, ...]
    assets: tuple[NonEmptyStr, ...]
    sectors: tuple[NonEmptyStr, ...] = ()
    horizon: NonEmptyStr
    base_magnitude: ExactDecimal = Field(ge=0, le=1)
    event_risk: Literal[
        "NORMAL", "CAUTION", "BLOCK_NEW_ENTRIES", "MARKET_EMERGENCY"
    ] = "NORMAL"
    positive_sentiment_direction: Literal[
        "BULLISH", "BEARISH", "NEUTRAL", "UNCERTAIN"
    ] = "UNCERTAIN"
    negative_sentiment_direction: Literal[
        "BULLISH", "BEARISH", "NEUTRAL", "UNCERTAIN"
    ] = "UNCERTAIN"


class NewsScoringConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_tier_weights: dict[NewsSourceTier, ExactDecimal]
    feature_weights: dict[NonEmptyStr, ExactDecimal]
    freshness_half_life_seconds: int = Field(gt=0)
    max_item_age_seconds: int = Field(gt=0)
    snapshot_ttl_seconds: int = Field(gt=0)
    risk_caution_threshold: ExactDecimal = Field(ge=0, le=1)
    risk_block_threshold: ExactDecimal = Field(ge=0, le=1)
    risk_emergency_threshold: ExactDecimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _weights_and_thresholds_are_valid(self) -> NewsScoringConfig:
        expected_tiers = {tier.value for tier in NewsSourceTier}
        if set(self.source_tier_weights) != expected_tiers:
            raise ValueError("source weights must cover each source tier exactly")
        if any(
            not Decimal("0") <= weight <= Decimal("1")
            for weight in self.source_tier_weights.values()
        ):
            raise ValueError("source tier weights must be between 0 and 1")
        if any(weight < 0 for weight in self.feature_weights.values()):
            raise ValueError("scoring feature weights must be non-negative")
        if sum(self.feature_weights.values()) != 1:
            raise ValueError("scoring feature weights must sum exactly to 1")
        if not (
            self.risk_caution_threshold
            < self.risk_block_threshold
            < self.risk_emergency_threshold
        ):
            raise ValueError("risk thresholds must increase from caution to emergency")
        return self


class NewsScheduleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timezone: Literal["Asia/Kolkata"]
    premarket: str
    midday: str
    pre_cas: str
    postmarket: str
    weekly_proposal: str
    monday_invalidation: str

    @field_validator("premarket", "midday", "pre_cas", "postmarket")
    @classmethod
    def _clock_time_format(cls, value: str) -> str:
        time.fromisoformat(value)
        return value


class NewsSubsystemConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: NonEmptyStr
    config_version: NonEmptyStr
    max_headline_chars: int = Field(gt=0, le=2000)
    max_snippet_chars: int = Field(ge=0, le=8000)
    max_classifier_batch_size: int = Field(gt=0, le=256)
    max_classifier_tokens: int = Field(gt=0, le=4096)
    sentiment_provider: SentimentProvider = "unavailable"
    finbert_model_path: str = ""
    event_cluster_window_seconds: int = Field(default=86400, gt=0)
    event_cluster_similarity: ExactDecimal = Field(default=Decimal("0.55"), ge=0, le=1)
    sources: tuple[NewsSourceConfig, ...]
    taxonomy: tuple[NewsTaxonomyRule, ...]
    scoring: NewsScoringConfig
    schedule: NewsScheduleConfig

    @model_validator(mode="after")
    def _source_and_taxonomy_ids_unique(self) -> NewsSubsystemConfig:
        source_ids = [source.source_id for source in self.sources]
        event_types = [rule.event_type for rule in self.taxonomy]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("news source IDs must be unique")
        if len(event_types) != len(set(event_types)):
            raise ValueError("taxonomy event types must be unique")
        if self.sentiment_provider == "finbert" and not self.finbert_model_path:
            raise ValueError("FinBERT requires an explicit local model path")
        return self


def load_news_config(path: Path) -> NewsSubsystemConfig:
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping")
    return NewsSubsystemConfig.model_validate(raw)
