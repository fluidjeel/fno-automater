"""Contract/config tests for the advisory news subsystem."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading.domain.enums import DataQuality
from trading.news.config import load_news_config
from trading.news.contracts import (
    AssetImpact,
    AssetImpactDirection,
    EventRiskState,
    EventRiskStatus,
    MacroProposal,
    MacroRegime,
    NewsEvent,
    NewsItem,
    NewsQuality,
    NewsSourceTier,
    ProposalEvidence,
    SentimentProbabilities,
    SentimentSnapshot,
)
from trading.news.taxonomy import classify_taxonomy

NOW = datetime(2026, 9, 13, 3, 0, tzinfo=UTC)
CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "news.yaml"


def _news_item(**overrides: object) -> NewsItem:
    fields: dict[str, object] = {
        "news_item_id": "NEWS-1",
        "source_id": "rbi_press_releases",
        "source_tier": NewsSourceTier.OFFICIAL,
        "canonical_url": "https://example.gov/news/1",
        "url_hash": "b" * 64,
        "title_hash": "c" * 64,
        "headline": "RBI announces monetary policy decision",
        "snippet": "Repo rate and liquidity decision.",
        "language": "en",
        "published_at": NOW,
        "retrieved_at": NOW + timedelta(seconds=1),
        "content_hash": "a" * 64,
        "quality_state": NewsQuality.VALID,
    }
    fields.update(overrides)
    return NewsItem.model_validate(fields)


def _impact() -> AssetImpact:
    return AssetImpact(
        asset="NSE_INDEX",
        direction=AssetImpactDirection.UNCERTAIN,
        magnitude=Decimal("0.8"),
        relevance=Decimal("0.9"),
        confidence=Decimal("0.7"),
        horizon="days",
        source_weight=Decimal("1"),
        sentiment_probabilities=SentimentProbabilities(
            positive=Decimal("0.2"),
            negative=Decimal("0.2"),
            neutral=Decimal("0.6"),
        ),
        evidence_ids=("NEWS-1",),
        calculation_version="scoring-v1",
        expires_at=NOW + timedelta(hours=1),
    )


def test_all_contracts_round_trip_without_losing_versions() -> None:
    item = _news_item()
    event = NewsEvent(
        event_id="EVENT-1",
        event_type="RBI_POLICY_LIQUIDITY",
        entities=("RBI",),
        assets=("NSE_INDEX",),
        first_seen_at=NOW,
        latest_seen_at=NOW + timedelta(minutes=1),
        evidence_item_ids=(item.news_item_id,),
        original_source_id=item.source_id,
        duplicate_count=0,
        confirmation_count=1,
        novelty=Decimal("1"),
        freshness=Decimal("1"),
        expires_at=NOW + timedelta(hours=1),
        quality_state=NewsQuality.VALID,
    )
    snapshot = SentimentSnapshot(
        snapshot_id="SNAP-1",
        as_of=NOW,
        expires_at=NOW + timedelta(minutes=15),
        asset_impacts=(_impact(),),
        event_cluster_ids=(event.event_id,),
        source_coverage={item.source_id: 1},
        missing_source_ids=(),
        contradiction_indicators=(),
        model_version="finbert-local-v1",
        config_version="news-config-v1",
        config_checksum="sha256:abc",
        quality_state=DataQuality.VALID,
        lineage=(item.content_hash,),
    )
    risk = EventRiskState(
        scope="NSE_INDEX",
        state=EventRiskStatus.BLOCK_NEW_ENTRIES,
        as_of=NOW,
        expires_at=NOW + timedelta(minutes=15),
        event_ids=(event.event_id,),
        reason_codes=("HIGH_IMPACT_RBI_EVENT",),
        quality_state=NewsQuality.VALID,
    )
    proposal = MacroProposal(
        proposal_id="PROPOSAL-1",
        as_of=NOW,
        valid_until=NOW + timedelta(days=7),
        regime=MacroRegime.RISK_OFF,
        allowed_strategy_families=(),
        blocked_strategy_families=("positional_index_options",),
        evidence=(
            ProposalEvidence(
                evidence_id=item.news_item_id,
                source_id=item.source_id,
                retrieved_at=item.retrieved_at,
                canonical_url=item.canonical_url,
            ),
        ),
        contradictions=(),
        assumptions=(),
        abstain=False,
        rationale="Evidence indicates elevated uncertainty.",
        model_version="macro-agent-v1",
        prompt_version="prompt-v1",
        retrieval_version="retrieval-v1",
        policy_version="policy-v1",
    )
    for contract in (item, event, _impact(), snapshot, risk, proposal):
        assert (
            type(contract).model_validate(contract.model_dump(mode="json")) == contract
        )


def test_news_rejects_naive_times_and_bad_hashes() -> None:
    with pytest.raises(ValidationError, match="naive"):
        _news_item(published_at=datetime(2026, 9, 13))
    with pytest.raises(ValidationError, match="pattern"):
        _news_item(content_hash="not-a-sha256")


def test_probabilities_must_sum_to_one() -> None:
    with pytest.raises(ValidationError, match="sum exactly to 1"):
        SentimentProbabilities(
            positive=Decimal("0.5"),
            negative=Decimal("0.2"),
            neutral=Decimal("0.2"),
        )


def test_macro_proposal_requires_evidence_unless_abstaining() -> None:
    with pytest.raises(ValidationError, match="requires grounded evidence"):
        MacroProposal(
            proposal_id="P-1",
            as_of=NOW,
            valid_until=NOW + timedelta(days=7),
            regime=MacroRegime.RISK_ON,
            allowed_strategy_families=(),
            blocked_strategy_families=(),
            evidence=(),
            contradictions=(),
            assumptions=(),
            abstain=False,
            rationale="",
            model_version="m1",
            prompt_version="p1",
            retrieval_version="r1",
            policy_version="v1",
        )


def test_event_risk_is_a_read_only_advisory_contract() -> None:
    fields = set(EventRiskState.model_fields)
    assert fields.isdisjoint(
        {"order", "quantity", "stop", "close_position", "flatten", "broker_command"}
    )


def test_research_config_loads_and_maps_rbi_event_taxonomy() -> None:
    config = load_news_config(CONFIG_PATH)
    assert any(source.source_id == "gdelt_global" for source in config.sources)
    assert any(
        source.source_id == "fred_indicators"
        and source.enabled
        and source.api_key_env == "FRED_API_KEY"
        for source in config.sources
    )
    item = _news_item()
    matches = classify_taxonomy(item, config)
    assert matches[0].rule.event_type == "RBI_POLICY_LIQUIDITY"
    assert matches[0].matched_keywords
