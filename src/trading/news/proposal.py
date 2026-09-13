"""Grounded weekly proposal boundary with a safe default abstention."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Protocol

from pydantic import model_validator

from trading.domain.contracts.base import NonEmptyStr, UtcDatetime, VersionedModel
from trading.news.contracts import (
    MacroProposal,
    MacroRegime,
    NewsEvent,
    NewsItem,
    ProposalEvidence,
    SentimentSnapshot,
)

__all__ = [
    "MacroAgentInput",
    "MacroAgentPort",
    "abstaining_proposal",
    "validate_proposal",
]


class MacroAgentInput(VersionedModel):
    """Only clustered, timestamped records reach an optional proposal agent."""

    request_id: NonEmptyStr
    as_of: UtcDatetime
    snapshot: SentimentSnapshot
    event_clusters: tuple[NewsEvent, ...]
    evidence_items: tuple[NewsItem, ...]

    @model_validator(mode="after")
    def _input_is_aggregated_and_point_in_time(self) -> MacroAgentInput:
        known_items = {item.news_item_id for item in self.evidence_items}
        known_events = {event.event_id for event in self.event_clusters}
        clustered_items = {
            item_id
            for event in self.event_clusters
            for item_id in event.evidence_item_ids
        }
        titles = [item.title_hash for item in self.evidence_items]
        if known_items != clustered_items or len(titles) != len(set(titles)):
            raise ValueError(
                "macro input must contain only deduplicated event evidence"
            )
        if not known_events.issubset(set(self.snapshot.event_cluster_ids)):
            raise ValueError("macro input events must be represented in the snapshot")
        if any(
            not set(event.evidence_item_ids).issubset(known_items)
            for event in self.event_clusters
        ):
            raise ValueError("macro input event evidence is incomplete")
        if any(item.published_at > self.as_of for item in self.evidence_items):
            raise ValueError("macro input cannot contain future evidence")
        if self.snapshot.as_of > self.as_of:
            raise ValueError("macro input snapshot is newer than its as_of")
        return self


class MacroAgentPort(Protocol):
    """Optional agent implementation returns strict proposal records."""

    def propose(self, evidence: MacroAgentInput) -> MacroProposal: ...


def abstaining_proposal(
    *, as_of: datetime, items: tuple[NewsItem, ...] = ()
) -> MacroProposal:
    evidence = tuple(
        ProposalEvidence(
            evidence_id=item.news_item_id,
            source_id=item.source_id,
            retrieved_at=item.retrieved_at,
            canonical_url=item.canonical_url,
        )
        for item in items
    )
    proposal_id = hashlib.sha256(
        (as_of.isoformat() + "|" + "|".join(e.evidence_id for e in evidence)).encode()
    ).hexdigest()
    return MacroProposal(
        proposal_id=proposal_id,
        as_of=as_of,
        valid_until=as_of + timedelta(days=7),
        regime=MacroRegime.ABSTAIN,
        allowed_strategy_families=(),
        blocked_strategy_families=(),
        evidence=evidence,
        contradictions=(),
        assumptions=("No approved proposal generator is configured.",),
        abstain=True,
        rationale="Abstain by default; this record grants no trading authority.",
        model_version="none",
        prompt_version="none",
        retrieval_version="none",
        policy_version="proposal-v1",
    )


def validate_proposal(payload: dict[str, object]) -> MacroProposal:
    """Parse untrusted generator output through the strict proposal contract."""
    return MacroProposal.model_validate(payload)
