"""POSITION desk advice contract (ADESK-B3).

SHADOW-only alongside deterministic review.
"""

from __future__ import annotations

from trading.domain.contracts.base import NonEmptyStr, UtcDatetime, VersionedModel
from trading.domain.enums import AgentAction, ReviewAction, ReviewSlotId

__all__ = ["ALLOWED_POSITION_ACTIONS", "PositionAdvice"]

ALLOWED_POSITION_ACTIONS: frozenset[AgentAction] = frozenset(
    {
        AgentAction.HOLD,
        AgentAction.TIGHTEN_STOP,
        AgentAction.PARTIAL_EXIT,
        AgentAction.FULL_EXIT,
        AgentAction.ABSTAIN,
    }
)


class PositionAdvice(VersionedModel):
    """Shadow POSITION desk output for one review slot. Never an order."""

    as_of: UtcDatetime
    snapshot_id: NonEmptyStr
    trade_id: NonEmptyStr
    slot_id: ReviewSlotId
    action: AgentAction
    mirrors_deterministic: ReviewAction
    packet_version: NonEmptyStr
    evidence_ids: tuple[NonEmptyStr, ...] = ()
    narrative: NonEmptyStr
