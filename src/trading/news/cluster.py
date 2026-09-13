"""Deterministic, provenance-aware headline deduplication and clustering."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading.news.config import NewsSubsystemConfig
from trading.news.contracts import NewsEvent, NewsItem, NewsQuality
from trading.news.taxonomy import classify_taxonomy

__all__ = ["cluster_news"]

_TOKEN = re.compile(r"[a-z0-9]+")
_TIER_PRIORITY = {
    "OFFICIAL": 0,
    "PRIMARY": 1,
    "MEDIA": 2,
    "AGGREGATOR": 3,
    "SOCIAL": 4,
    "UNKNOWN": 5,
}


def _similarity(left: str, right: str) -> Decimal:
    a, b = set(_TOKEN.findall(left.casefold())), set(_TOKEN.findall(right.casefold()))
    if not a or not b:
        return Decimal(0)
    return Decimal(len(a & b)) / Decimal(len(a | b))


def cluster_news(
    items: tuple[NewsItem, ...], *, as_of: datetime, config: NewsSubsystemConfig
) -> tuple[NewsEvent, ...]:
    """Group same-type, same-asset reports while collapsing syndicated copies."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    now = as_of.astimezone(UTC)
    eligible = sorted(
        (
            item
            for item in items
            if item.published_at <= now
            and (now - item.published_at).total_seconds()
            <= config.scoring.max_item_age_seconds
        ),
        key=lambda item: (item.published_at, item.news_item_id),
    )
    groups: dict[
        tuple[str, tuple[str, ...]], list[tuple[NewsItem, str, tuple[str, ...]]]
    ] = defaultdict(list)
    for item in eligible:
        matches = classify_taxonomy(item, config)
        if not matches:
            continue
        for match in matches:
            key = (match.rule.event_type, tuple(sorted(match.rule.assets)))
            groups[key].append((item, match.rule.horizon, match.matched_keywords))
    events: list[NewsEvent] = []
    for (event_type, assets), entries in sorted(groups.items()):
        clusters: list[list[tuple[NewsItem, str, tuple[str, ...]]]] = []
        for entry in entries:
            item = entry[0]
            for cluster in clusters:
                anchor = cluster[0][0]
                in_window = (
                    abs((item.published_at - anchor.published_at).total_seconds())
                    <= config.event_cluster_window_seconds
                )
                if (
                    in_window
                    and _similarity(item.headline, anchor.headline)
                    >= config.event_cluster_similarity
                ):
                    cluster.append(entry)
                    break
            else:
                clusters.append([entry])
        for cluster in clusters:
            unique_by_title: dict[str, tuple[NewsItem, str, tuple[str, ...]]] = {}
            for entry in cluster:
                item = entry[0]
                prior = unique_by_title.get(item.title_hash)
                if prior is None or (
                    _TIER_PRIORITY[item.source_tier.value],
                    item.source_id,
                ) < (_TIER_PRIORITY[prior[0].source_tier.value], prior[0].source_id):
                    unique_by_title[item.title_hash] = entry
            representatives = sorted(
                unique_by_title.values(),
                key=lambda value: (value[0].published_at, value[0].news_item_id),
            )
            evidence = tuple(value[0].news_item_id for value in representatives)
            times = [value[0].published_at for value in representatives]
            primary = min(
                representatives,
                key=lambda value: (
                    _TIER_PRIORITY[value[0].source_tier.value],
                    value[0].published_at,
                ),
            )[0]
            latest = max(times)
            expiry = latest + timedelta(seconds=config.scoring.max_item_age_seconds)
            event_seed = f"{event_type}|{'|'.join(assets)}|{'|'.join(sorted(evidence))}"
            event_id = hashlib.sha256(event_seed.encode()).hexdigest()
            rule = next(
                rule for rule in config.taxonomy if rule.event_type == event_type
            )
            age = max(0.0, (now - latest).total_seconds())
            freshness = Decimal(
                str(2 ** (-age / config.scoring.freshness_half_life_seconds))
            ).quantize(Decimal("0.000001"))
            novelty = Decimal(1) / Decimal(max(1, len(representatives)))
            duplicate_count = len(cluster) - len(representatives)
            events.append(
                NewsEvent(
                    event_id=event_id,
                    event_type=event_type,
                    entities=tuple(
                        sorted(
                            {word for _, _, words in representatives for word in words}
                        )
                    ),
                    assets=assets,
                    sectors=tuple(sorted(rule.sectors)),
                    first_seen_at=min(times),
                    latest_seen_at=latest,
                    evidence_item_ids=evidence,
                    original_source_id=primary.source_id,
                    duplicate_count=duplicate_count,
                    confirmation_count=len(representatives),
                    novelty=novelty,
                    freshness=freshness,
                    expires_at=expiry,
                    quality_state=NewsQuality.VALID,
                )
            )
    return tuple(
        sorted(events, key=lambda event: (event.first_seen_at, event.event_id))
    )
