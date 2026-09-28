"""PAPER promoted-strike depth and chain-cache configuration."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, model_validator

from trading.domain.contracts.base import ExactDecimal, StrictModel, VersionedModel

__all__ = [
    "DepthPromotionConfig",
    "DepthPromotionConfigError",
    "DepthScoreWeights",
    "RateLimitBackoffConfig",
    "load_depth_promotion_config",
]


class DepthPromotionConfigError(ValueError):
    """Raised when depth-promotion settings cannot be parsed."""


class DepthScoreWeights(StrictModel):
    """Blend weights for strike promotion scoring; must sum to 1."""

    open_interest: ExactDecimal = Field(gt=0)
    volume: ExactDecimal = Field(ge=0)
    proximity: ExactDecimal = Field(ge=0)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> DepthScoreWeights:
        total = self.open_interest + self.volume + self.proximity
        if total != Decimal("1"):
            raise ValueError("score weights must sum to 1")
        return self


class RateLimitBackoffConfig(StrictModel):
    """Exponential backoff for Fyers 429 responses."""

    base_seconds: ExactDecimal = Field(gt=0)
    cap_seconds: ExactDecimal = Field(gt=0)
    jitter_fraction: ExactDecimal = Field(ge=0, le=Decimal("1"))


class DepthPromotionConfig(VersionedModel):
    """Rate-limit-aware promoted-strike depth for the PAPER session."""

    schema_version: str = "1"
    depth_set_size: int = Field(default=8, ge=5, le=10)
    score_weights: DepthScoreWeights
    promote_after_polls: int = Field(default=2, ge=1)
    demote_after_polls: int = Field(default=3, ge=1)
    chain_cache_ttl_seconds: int = Field(default=90, ge=30)
    rate_limit_backoff: RateLimitBackoffConfig
    ws_channel: int = Field(default=11, ge=1)
    rest_depth_fallback_enabled: bool = True

    def round_trip(self) -> DepthPromotionConfig:
        return DepthPromotionConfig.model_validate(self.model_dump(mode="json"))


def load_depth_promotion_config(path: Path) -> DepthPromotionConfig:
    """Load ``depth_promotion`` from ``config/paper_data.yaml``."""
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DepthPromotionConfigError(f"cannot read {path}: {exc}") from exc
    try:
        payload: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise DepthPromotionConfigError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise DepthPromotionConfigError(f"{path}: expected a mapping at the top level")
    section = payload.get("depth_promotion")
    if not isinstance(section, dict):
        raise DepthPromotionConfigError(f"{path}: missing depth_promotion section")
    try:
        return DepthPromotionConfig.model_validate(section)
    except (ValueError, TypeError) as exc:
        raise DepthPromotionConfigError(f"{path}: {exc}") from exc
