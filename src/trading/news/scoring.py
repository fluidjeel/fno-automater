"""Explainable advisory asset impacts and deterministic event-risk states."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading.domain.enums import DataQuality
from trading.news.config import NewsSubsystemConfig
from trading.news.contracts import (
    AssetImpact,
    AssetImpactDirection,
    EventRiskState,
    EventRiskStatus,
    NewsEvent,
    NewsItem,
    SentimentLabel,
    SentimentProbabilities,
    SentimentSnapshot,
)
from trading.news.sentiment import SentimentClassifier, UnavailableSentimentClassifier

__all__ = ["build_snapshot"]

_MIN_INDEPENDENT_CONFIRMATIONS = 2


def build_snapshot(
    *,
    items: tuple[NewsItem, ...],
    events: tuple[NewsEvent, ...],
    as_of: datetime,
    config: NewsSubsystemConfig,
    classifier: SentimentClassifier | None = None,
) -> tuple[SentimentSnapshot, tuple[EventRiskState, ...]]:
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    now = as_of.astimezone(UTC)
    usable = tuple(
        item
        for item in items
        if item.published_at <= now
        and (now - item.published_at).total_seconds()
        <= config.scoring.max_item_age_seconds
    )
    engine = classifier or UnavailableSentimentClassifier()
    classifications = engine.classify(
        tuple(f"{item.headline}. {item.snippet}" for item in usable)
    )
    by_id = {
        item.news_item_id: (item, result)
        for item, result in zip(usable, classifications, strict=True)
    }
    impacts: list[AssetImpact] = []
    risks: list[EventRiskState] = []
    for event in events:
        evidence = [
            by_id[item_id] for item_id in event.evidence_item_ids if item_id in by_id
        ]
        if not evidence:
            continue
        probabilities = [
            result.probabilities
            for _, result in evidence
            if result.probabilities is not None
        ]
        contradiction = False
        if probabilities:
            positive = sum((p.positive for p in probabilities), Decimal(0)) / len(
                probabilities
            )
            negative = sum((p.negative for p in probabilities), Decimal(0)) / len(
                probabilities
            )
            neutral = Decimal(1) - positive - negative
        else:
            positive = negative = neutral = Decimal(0)
        labels = {result.label.value for _, result in evidence}
        contradiction = "POSITIVE" in labels and "NEGATIVE" in labels
        sentiment_confidence = (
            Decimal(0) if not probabilities else max(positive, negative, neutral)
        )
        signed = positive - negative
        direction = AssetImpactDirection.UNCERTAIN
        if probabilities and positive <= Decimal("0.2") and negative <= Decimal("0.2"):
            direction = AssetImpactDirection.NEUTRAL
        source_weights = {source.source_id: source.weight for source in config.sources}
        tier_weight = max(
            min(
                config.scoring.source_tier_weights[item.source_tier],
                source_weights.get(item.source_id, Decimal(0)),
            )
            for item, _ in evidence
        )
        rule = next(
            (r for r in config.taxonomy if r.event_type == event.event_type), None
        )
        if rule is None:
            continue
        if probabilities and abs(signed) >= Decimal("0.2"):
            mapped_direction = (
                rule.positive_sentiment_direction
                if signed > 0
                else rule.negative_sentiment_direction
            )
            direction = AssetImpactDirection(mapped_direction)
        relevance = rule.base_magnitude
        independent_confirmation = min(
            Decimal(1),
            Decimal(event.confirmation_count) / _MIN_INDEPENDENT_CONFIRMATIONS,
        )
        confidence = sentiment_confidence * tier_weight * independent_confirmation
        features = {
            "sentiment": abs(signed),
            "relevance": relevance,
            "source_credibility": tier_weight,
            "originality": Decimal(1) / Decimal(1 + event.duplicate_count),
            "novelty": event.novelty,
            "freshness": event.freshness,
            "confirmation": min(Decimal(1), Decimal(event.confirmation_count) / 3),
            "contradiction": Decimal(0) if contradiction else Decimal(1),
        }
        weighted_score = sum(
            (
                weight * features[name]
                for name, weight in config.scoring.feature_weights.items()
            ),
            Decimal(0),
        )
        magnitude = min(Decimal(1), weighted_score * relevance)
        expires = min(
            event.expires_at,
            now + timedelta(seconds=config.scoring.snapshot_ttl_seconds),
        )
        for asset in event.assets:
            impacts.append(
                AssetImpact(
                    asset=asset,
                    direction=direction,
                    magnitude=magnitude,
                    relevance=relevance,
                    confidence=confidence,
                    horizon=rule.horizon,
                    source_weight=tier_weight,
                    sentiment_probabilities=(
                        SentimentProbabilities(
                            positive=positive,
                            negative=negative,
                            neutral=neutral,
                        )
                        if probabilities
                        else None
                    ),
                    evidence_ids=event.evidence_item_ids,
                    calculation_version=config.config_version,
                    expires_at=max(expires, now + timedelta(seconds=1)),
                )
            )
        score = min(Decimal(1), rule.base_magnitude * weighted_score)
        risk_state = EventRiskStatus.NORMAL
        credible_event = (
            tier_weight >= Decimal("0.5")
            or event.confirmation_count >= _MIN_INDEPENDENT_CONFIRMATIONS
        )
        if not credible_event:
            risk_state = EventRiskStatus.CAUTION
        elif (
            rule.event_risk == "MARKET_EMERGENCY"
            or score >= config.scoring.risk_emergency_threshold
        ):
            risk_state = EventRiskStatus.MARKET_EMERGENCY
        elif (
            rule.event_risk == "BLOCK_NEW_ENTRIES"
            or score >= config.scoring.risk_block_threshold
        ):
            risk_state = EventRiskStatus.BLOCK_NEW_ENTRIES
        elif (
            rule.event_risk == "CAUTION"
            or score >= config.scoring.risk_caution_threshold
        ):
            risk_state = EventRiskStatus.CAUTION
        risks.extend(
            EventRiskState(
                scope=asset,
                state=risk_state,
                as_of=now,
                expires_at=max(expires, now + timedelta(seconds=1)),
                event_ids=(event.event_id,),
                reason_codes=(
                    (f"EVENT_{event.event_type}",)
                    if credible_event
                    else ("LOW_CREDIBILITY_EVENT",)
                ),
                quality_state=event.quality_state,
            )
            for asset in event.assets
        )
    key = hashlib.sha256(
        (
            now.isoformat()
            + "|"
            + config.config_version
            + "|"
            + engine.model_version
            + "|"
            + "|".join(sorted(e.event_id for e in events))
            + "|"
            + "|".join(sorted(i.news_item_id for i in usable))
        ).encode()
    ).hexdigest()
    snapshot = SentimentSnapshot(
        snapshot_id=key,
        as_of=now,
        expires_at=now + timedelta(seconds=config.scoring.snapshot_ttl_seconds),
        asset_impacts=tuple(impacts),
        event_cluster_ids=tuple(e.event_id for e in events),
        source_coverage={
            source.source_id: sum(item.source_id == source.source_id for item in usable)
            for source in config.sources
        },
        missing_source_ids=tuple(
            source.source_id
            for source in config.sources
            if source.enabled
            and not any(item.source_id == source.source_id for item in usable)
        ),
        contradiction_indicators=tuple(
            sorted(
                event.event_id
                for event in events
                if any(
                    by_id[item_id][1].label is SentimentLabel.POSITIVE
                    for item_id in event.evidence_item_ids
                    if item_id in by_id
                )
                and any(
                    by_id[item_id][1].label is SentimentLabel.NEGATIVE
                    for item_id in event.evidence_item_ids
                    if item_id in by_id
                )
            )
        ),
        model_version=engine.model_version,
        config_version=config.config_version,
        config_checksum=hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
        quality_state=DataQuality.VALID if usable else DataQuality.DEGRADED,
        lineage=tuple(sorted(item.content_hash for item in usable)),
    )
    return snapshot, tuple(risks)
