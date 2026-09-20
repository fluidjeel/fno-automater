"""Logged Agent Desk decision. Append-only audit; never a broker instruction.

Invariant 2: this record cannot place, amend or cancel an order. Strategies and
desks still emit intents/proposals only; Layer 2 remains the only live path.
ADESK-A2 stores the measurement substrate PART 14 queries; it does not call an
LLM and does not change C1 BOUNDED rules.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import (
    AgentAction,
    AuthorityMode,
    DeskRole,
    Environment,
    GateOutcome,
)

__all__ = ["AgentDecision"]


class AgentDecision(VersionedModel):
    """One desk output after validation, with the authority mode at call time."""

    decision_id: NonEmptyStr
    run_id: NonEmptyStr
    role: DeskRole
    mode: AuthorityMode
    environment: Environment
    trade_id: NonEmptyStr | None = None
    snapshot_id: NonEmptyStr
    action: AgentAction
    confidence: ExactDecimal | None = Field(default=None, ge=Decimal(0), le=Decimal(1))
    size_multiplier: ExactDecimal | None = Field(default=None, ge=Decimal(0))
    deterministic_choice: NonEmptyStr | None = None
    agent_override: StrictBool
    reason_codes: tuple[NonEmptyStr, ...]
    ungrounded_codes: tuple[NonEmptyStr, ...]
    evidence_ids: tuple[NonEmptyStr, ...]
    gate_outcome: GateOutcome
    gate_reject_codes: tuple[NonEmptyStr, ...] | None = None
    model_id: NonEmptyStr
    prompt_version: NonEmptyStr
    policy_version: NonEmptyStr
    packet_version: NonEmptyStr
    input_tokens: StrictInt = Field(ge=0)
    output_tokens: StrictInt = Field(ge=0)
    latency_ms: StrictInt = Field(ge=0)
    created_at: UtcDatetime
