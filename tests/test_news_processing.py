"""Offline acceptance tests for the news processing vertical slices."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from trading.news import sentiment as sentiment_module
from trading.news.cluster import cluster_news
from trading.news.config import load_news_config
from trading.news.contracts import (
    EventRiskState,
    NewsItem,
    NewsQuality,
    NewsSourceTier,
    SentimentLabel,
)
from trading.news.proposal import MacroAgentInput, abstaining_proposal
from trading.news.scoring import build_snapshot
from trading.news.sentiment import (
    DeterministicTestClassifier,
    FinBertClassifier,
    UnavailableSentimentClassifier,
)
from trading.news.sources import CollectionBatch, NewsCollector, SourceHealth
from trading.news.storage import NewsJsonlStore

NOW = datetime(2026, 9, 13, 3, 0, tzinfo=UTC)
CONFIG = load_news_config(Path(__file__).resolve().parents[1] / "config/news.yaml")


def item(item_id: str, source_id: str, tier: NewsSourceTier, headline: str) -> NewsItem:
    return NewsItem(
        news_item_id=item_id,
        source_id=source_id,
        source_tier=tier,
        canonical_url=f"https://{source_id}.example/{item_id}",
        url_hash=(item_id.encode().hex() * 64)[:64],
        title_hash=hashlib.sha256(headline.casefold().encode()).hexdigest(),
        headline=headline,
        snippet="",
        language="en",
        published_at=NOW - timedelta(minutes=5),
        retrieved_at=NOW,
        content_hash=(item_id.encode().hex() * 64)[:64],
        quality_state=NewsQuality.VALID,
    )


def test_syndicated_headlines_do_not_inflate_confirmations() -> None:
    copies = (
        item(
            "one",
            "gdelt_global",
            NewsSourceTier.AGGREGATOR,
            "RBI announces monetary policy decision",
        ),
        item(
            "two",
            "rbi_press_releases",
            NewsSourceTier.OFFICIAL,
            "RBI announces monetary policy decision",
        ),
    )
    events = cluster_news(copies, as_of=NOW, config=CONFIG)
    assert len(events) == 1
    assert events[0].duplicate_count == 1
    assert events[0].confirmation_count == 1
    assert events[0].original_source_id == "rbi_press_releases"


def test_classifier_unknown_is_not_silently_neutral() -> None:
    assert (
        UnavailableSentimentClassifier().classify(("headline",))[0].label
        is SentimentLabel.UNKNOWN
    )


def test_single_low_credibility_report_cannot_trigger_high_confidence_risk() -> None:
    report = item(
        "social-one",
        "anonymous_social",
        NewsSourceTier.SOCIAL,
        "RBI announces monetary policy decision",
    )
    events = cluster_news((report,), as_of=NOW, config=CONFIG)
    snapshot, risks = build_snapshot(
        items=(report,),
        events=events,
        as_of=NOW,
        config=CONFIG,
        classifier=DeterministicTestClassifier(),
    )
    assert all(impact.confidence <= Decimal("0.1") for impact in snapshot.asset_impacts)
    assert all(risk.state.value == "CAUTION" for risk in risks)
    assert all("LOW_CREDIBILITY_EVENT" in risk.reason_codes for risk in risks)


def test_direction_uses_only_configured_event_sentiment_mapping() -> None:
    report = item(
        "earnings-one",
        "rbi_press_releases",
        NewsSourceTier.OFFICIAL,
        "Quarterly earnings beat estimates with growth",
    )
    events = cluster_news((report,), as_of=NOW, config=CONFIG)
    snapshot, _ = build_snapshot(
        items=(report,),
        events=events,
        as_of=NOW,
        config=CONFIG,
        classifier=DeterministicTestClassifier(),
    )
    assert snapshot.asset_impacts[0].direction.value == "BULLISH"
    assert (
        DeterministicTestClassifier().classify(("earnings beat estimates",))[0].label
        is SentimentLabel.POSITIVE
    )


def test_finbert_model_failure_is_explicit_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(_: str) -> object:
        raise ImportError("optional model package not installed")

    monkeypatch.setattr(sentiment_module, "import_module", unavailable)
    model = FinBertClassifier("/models/ProsusAI-finbert")
    result = model.classify(("RBI announcement",))[0]
    assert result.label is SentimentLabel.UNKNOWN
    assert result.probabilities is None
    assert "MODEL_UNAVAILABLE:ImportError" in result.model_version


def test_finbert_batches_respect_configured_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch_sizes: list[int] = []

    def infer(texts: list[str]) -> list[list[dict[str, object]]]:
        batch_sizes.append(len(texts))
        return [
            [
                {"label": "positive", "score": 0.8},
                {"label": "negative", "score": 0.1},
                {"label": "neutral", "score": 0.1},
            ]
            for _ in texts
        ]

    def factory(*_: object, **__: object) -> object:
        return infer

    monkeypatch.setattr(
        sentiment_module,
        "import_module",
        lambda _: SimpleNamespace(pipeline=factory),
    )
    model = FinBertClassifier("/models/finbert", batch_size=2)
    results = model.classify(("one", "two", "three"))
    assert batch_sizes == [2, 1]
    assert len(results) == 3
    assert results[0].probabilities is not None


def test_snapshot_and_event_risk_are_deterministic_and_advisory() -> None:
    evidence = (
        item(
            "one",
            "rbi_press_releases",
            NewsSourceTier.OFFICIAL,
            "RBI announces monetary policy decision",
        ),
    )
    events = cluster_news(evidence, as_of=NOW, config=CONFIG)
    first = build_snapshot(
        items=evidence,
        events=events,
        as_of=NOW,
        config=CONFIG,
        classifier=DeterministicTestClassifier(),
    )
    second = build_snapshot(
        items=evidence,
        events=events,
        as_of=NOW,
        config=CONFIG,
        classifier=DeterministicTestClassifier(),
    )
    assert first == second
    assert first[0].asset_impacts
    assert all(
        "position" not in field
        for risk in first[1]
        for field in EventRiskState.model_fields
    )
    unknown, _ = build_snapshot(items=evidence, events=events, as_of=NOW, config=CONFIG)
    assert unknown.asset_impacts[0].confidence == Decimal(0)
    assert unknown.asset_impacts[0].sentiment_probabilities is None


def test_jsonl_persistence_is_idempotent_and_proposal_abstains(tmp_path: Path) -> None:
    store = NewsJsonlStore(tmp_path)
    record = item(
        "one",
        "rbi_press_releases",
        NewsSourceTier.OFFICIAL,
        "RBI announces monetary policy decision",
    )
    assert store.append_items((record,)) == 1
    assert store.append_items((record,)) == 0
    assert store.count("items") == 1
    proposal = abstaining_proposal(as_of=NOW)
    assert proposal.abstain
    assert store.append_proposal(proposal) == 1
    assert store.append_proposal(proposal) == 0


def test_macro_agent_receives_only_clustered_point_in_time_evidence() -> None:
    record = item(
        "one",
        "rbi_press_releases",
        NewsSourceTier.OFFICIAL,
        "RBI announces monetary policy decision",
    )
    events = cluster_news((record,), as_of=NOW, config=CONFIG)
    snapshot, _ = build_snapshot(
        items=(record,), events=events, as_of=NOW, config=CONFIG
    )
    request = MacroAgentInput(
        request_id="macro-request-1",
        as_of=NOW,
        snapshot=snapshot,
        event_clusters=events,
        evidence_items=(record,),
    )
    assert request.event_clusters == events
    with pytest.raises(ValueError, match="only deduplicated"):
        MacroAgentInput(
            request_id="macro-request-2",
            as_of=NOW,
            snapshot=snapshot,
            event_clusters=events,
            evidence_items=(
                record,
                record.model_copy(update={"news_item_id": "duplicate"}),
            ),
        )


def test_collection_health_audit_is_idempotent(tmp_path: Path) -> None:
    batch = CollectionBatch(
        cycle_id="cycle-1",
        as_of=NOW,
        items=(),
        source_health=(SourceHealth("source", "OK", 0, 0, NOW),),
    )
    store = NewsJsonlStore(tmp_path)
    assert store.append_cycle(batch) == 1
    assert store.append_cycle(batch) == 0
    assert store.latest_cycle() is not None


def test_full_offline_collection_to_persisted_snapshot(tmp_path: Path) -> None:
    source = next(
        source for source in CONFIG.sources if source.source_id == "rbi_press_releases"
    )
    config = CONFIG.model_copy(update={"sources": (source,)})
    fixture = Path(__file__).resolve().parent / "fixtures/news/rbi_press_releases.xml"
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=fixture.read_bytes())
    )
    batch = NewsCollector(config, transport=transport).collect(as_of=NOW)
    events = cluster_news(batch.items, as_of=NOW, config=config)
    snapshot, risks = build_snapshot(
        items=batch.items,
        events=events,
        as_of=NOW,
        config=config,
        classifier=UnavailableSentimentClassifier(),
    )
    store = NewsJsonlStore(tmp_path)
    assert store.append_cycle(batch) == 1
    assert store.append_items(batch.items) == 1
    assert store.append_events(events) == 1
    assert store.append_snapshot(snapshot) == 1
    assert store.append_risks(risks) == len(risks)
    assert store.count("cycles") == 1
    assert store.count("items") == 1
    assert store.count("events") == 1
    assert store.count("snapshots") == 1
    assert store.count("risks") == len(risks)
