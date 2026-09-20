"""HallucinationEvent contract (ADESK-A8 / PART 11.1)."""

from __future__ import annotations

from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictBool,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import AgentAction, AgentReasonCode, DeskRole

__all__ = ["GroundingResult", "HallucinationEvent"]


class GroundingResult(VersionedModel):
    """Outcome of deterministic reason-code precondition checks."""

    grounded_codes: tuple[AgentReasonCode, ...]
    ungrounded_codes: tuple[AgentReasonCode, ...]
    original_action: AgentAction
    effective_action: AgentAction
    downgraded: StrictBool


class HallucinationEvent(VersionedModel):
    """Recorded when an agent cites a reason code its evidence does not support."""

    event_id: NonEmptyStr
    decision_id: NonEmptyStr
    role: DeskRole
    model_id: NonEmptyStr
    prompt_version: NonEmptyStr
    ungrounded_codes: tuple[AgentReasonCode, ...]
    original_action: AgentAction
    effective_action: AgentAction
    created_at: UtcDatetime
