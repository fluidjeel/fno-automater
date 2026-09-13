"""Deterministic scoring of curated, timestamped macro-news evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

__all__ = [
    "JsonlMacroNewsFeed",
    "MacroNewsFactor",
    "MacroNewsItem",
    "MacroNewsLoadResult",
    "format_macro_summary",
    "load_macro_news_jsonl",
    "merge_factor_into_payload",
    "score_macro_news",
]


class MacroNewsItem(BaseModel):
    """A pre-classified news item with its publication and capture evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    published_at: datetime
    received_at: datetime
    sentiment: Literal[-1, 0, 1]
    impact: Decimal = Field(ge=0, le=1)
    confidence: Decimal = Field(ge=0, le=1)
    evidence_ref: str = Field(min_length=1)

    @field_validator("published_at", "received_at")
    @classmethod
    def _timestamps_are_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("macro-news timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("received_at")
    @classmethod
    def _received_after_publication(cls, value: datetime, info: object) -> datetime:
        published = getattr(info, "data", {}).get("published_at")
        if published is not None and value < published:
            raise ValueError("received_at cannot precede published_at")
        return value


class MacroNewsFactor(BaseModel):
    """Bounded factor output and the evidence used to calculate it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sentiment: Decimal = Field(ge=-1, le=1)
    coverage: Decimal = Field(ge=0)
    event_count: int = Field(ge=0)
    evidence_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    evidence_items: tuple[MacroNewsItem, ...] = ()
    calculated_at: datetime
    calculation_version: str = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class MacroNewsLoadResult:
    """Outcome of parsing a macro-news JSONL file."""

    items: tuple[MacroNewsItem, ...]
    errors: tuple[str, ...]
    skipped_duplicates: tuple[str, ...]


def load_macro_news_jsonl(path: Path) -> MacroNewsLoadResult:
    """Parse JSONL; skip bad lines and duplicate event_ids (first wins)."""
    if not path.is_file():
        return MacroNewsLoadResult(items=(), errors=(), skipped_duplicates=())
    items: list[MacroNewsItem] = []
    errors: list[str] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for line_no, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        text = line.strip()
        if not text:
            continue
        try:
            item = MacroNewsItem.model_validate_json(text)
        except (ValidationError, ValueError) as exc:
            errors.append(f"line {line_no}: {exc}")
            continue
        if item.event_id in seen:
            skipped.append(item.event_id)
            continue
        seen.add(item.event_id)
        items.append(item)
    return MacroNewsLoadResult(
        items=tuple(items),
        errors=tuple(errors),
        skipped_duplicates=tuple(skipped),
    )


class JsonlMacroNewsFeed:
    """Read append-only structured classifications from a local JSONL feed."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._last_load: MacroNewsLoadResult | None = None

    def items(self) -> tuple[MacroNewsItem, ...]:
        self._last_load = load_macro_news_jsonl(self._path)
        return self._last_load.items

    @property
    def last_load(self) -> MacroNewsLoadResult | None:
        return self._last_load


def score_macro_news(
    items: tuple[MacroNewsItem, ...],
    *,
    scope: str,
    as_of: datetime,
    max_age_seconds: int,
    half_life_seconds: int,
    calculation_version: str,
) -> MacroNewsFactor | None:
    """Return a recency-weighted sentiment score without future or stale inputs."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    if max_age_seconds <= 0 or half_life_seconds <= 0:
        raise ValueError("macro-news age and half-life must be positive")
    instant = as_of.astimezone(UTC)
    weighted_sentiment = Decimal(0)
    total_weight = Decimal(0)
    evidence: list[MacroNewsItem] = []
    for item in items:
        if item.scope not in {scope, "GLOBAL"}:
            continue
        age = instant - item.published_at
        age_seconds = Decimal(age.days * 86_400 + age.seconds) + Decimal(
            age.microseconds
        ) / Decimal(1_000_000)
        if (
            age_seconds < 0
            or item.received_at > instant
            or age_seconds > max_age_seconds
        ):
            continue
        decay = max(
            Decimal(0),
            Decimal(1) - age_seconds / Decimal(half_life_seconds),
        )
        weight = item.impact * item.confidence * decay
        if weight <= 0:
            continue
        weighted_sentiment += Decimal(item.sentiment) * weight
        total_weight += weight
        evidence.append(item)
    if total_weight == 0:
        return None
    return MacroNewsFactor(
        sentiment=weighted_sentiment / total_weight,
        coverage=min(Decimal(1), total_weight),
        event_count=len(evidence),
        evidence_ids=tuple(item.event_id for item in evidence),
        evidence_refs=tuple(item.evidence_ref for item in evidence),
        evidence_items=tuple(evidence),
        calculated_at=instant,
        calculation_version=calculation_version,
    )


def merge_factor_into_payload(
    payload: dict[str, object],
    factor: MacroNewsFactor | None,
) -> dict[str, object]:
    """Embed scored macro news into a canonical event payload for replay."""
    merged = dict(payload)
    if factor is not None:
        merged["macro_news_factor"] = json.loads(factor.model_dump_json())
    return merged


def format_macro_summary(factor: MacroNewsFactor | None) -> str:
    """One-line macro factor status for CLI output."""
    if factor is None:
        return "macro=none"
    sentiment = factor.sentiment.quantize(Decimal("0.01"))
    return f"macro={factor.event_count}events sentiment={sentiment}"
