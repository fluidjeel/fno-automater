"""Event-risk snapshot for the unattended paper loop."""

from __future__ import annotations

from datetime import datetime, timedelta

from trading.news.cluster import cluster_news
from trading.news.config import NewsSubsystemConfig
from trading.news.contracts import EventRiskState, EventRiskStatus, NewsQuality
from trading.news.scoring import build_snapshot
from trading.news.sentiment import SentimentClassifier, UnavailableSentimentClassifier
from trading.news.sources import NewsCollector

__all__ = ["clear_event_risk", "collect_event_risk"]


def clear_event_risk(*, as_of: datetime, ttl_seconds: int) -> EventRiskState:
    """GLOBAL NORMAL after a successful empty collect. Missing collect is None."""
    return EventRiskState(
        scope="GLOBAL",
        state=EventRiskStatus.NORMAL,
        as_of=as_of,
        expires_at=as_of + timedelta(seconds=max(ttl_seconds, 1)),
        event_ids=(),
        reason_codes=("NO_MATERIAL_EVENTS",),
        quality_state=NewsQuality.VALID,
    )


def collect_event_risk(
    collector: NewsCollector,
    config: NewsSubsystemConfig,
    *,
    as_of: datetime,
    classifier: SentimentClassifier | None = None,
) -> EventRiskState | None:
    """Score news. Failed collection returns None so Layer 2 blocks entries."""
    try:
        batch = collector.collect(as_of=as_of)
    except (OSError, ValueError, RuntimeError):
        return None
    events = cluster_news(batch.items, as_of=as_of, config=config)
    _snapshot, risks = build_snapshot(
        items=batch.items,
        events=events,
        as_of=as_of,
        config=config,
        classifier=classifier or UnavailableSentimentClassifier(),
    )
    if not risks:
        return clear_event_risk(
            as_of=as_of,
            ttl_seconds=config.scoring.snapshot_ttl_seconds,
        )
    for risk in risks:
        if risk.scope == "GLOBAL":
            return risk
    return risks[0]
