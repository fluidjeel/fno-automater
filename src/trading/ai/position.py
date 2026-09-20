"""POSITION desk SHADOW runner (ADESK-B3).

Logs shadow advice from a DeltaPacket alongside the deterministic review.
Never influences stops, exits, or OMS. No LLM on the intraday path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import uuid4

from trading.ai.decision_log import DecisionLog
from trading.ai.packets import DeltaPacket
from trading.domain.contracts.agent_decision import AgentDecision
from trading.domain.contracts.position_advice import (
    ALLOWED_POSITION_ACTIONS,
    PositionAdvice,
)
from trading.domain.enums import (
    AgentAction,
    AuthorityMode,
    DeskRole,
    Environment,
    GateOutcome,
    ReviewAction,
    ReviewSlotId,
)

__all__ = [
    "POSITION_PACKET_VERSION",
    "POSITION_PROMPT_VERSION",
    "PositionShadowResult",
    "build_shadow_position_advice",
    "map_review_action_to_agent",
    "maybe_log_position_shadow",
]

POSITION_PROMPT_VERSION = "position-shadow-v1"
POSITION_PACKET_VERSION = "position-packet-v1"
POSITION_POLICY_VERSION = "position-policy-v1"
POSITION_MODEL_ID = "deterministic-shadow"


@dataclass(frozen=True, slots=True)
class PositionShadowResult:
    status: Literal["LOGGED", "SKIPPED_DISABLED"]
    advice: PositionAdvice | None
    decision: AgentDecision | None


def map_review_action_to_agent(action: ReviewAction) -> AgentAction:
    """Map deterministic ReviewAction onto AgentAction for shadow logging."""
    mapping = {
        ReviewAction.HOLD: AgentAction.HOLD,
        ReviewAction.TIGHTEN_STOP: AgentAction.TIGHTEN_STOP,
        ReviewAction.PARTIAL_EXIT: AgentAction.PARTIAL_EXIT,
        ReviewAction.FULL_EXIT: AgentAction.FULL_EXIT,
        ReviewAction.PROPOSE_HEDGE: AgentAction.ABSTAIN,
        ReviewAction.PROPOSE_ROLL: AgentAction.ABSTAIN,
    }
    return mapping[action]


def build_shadow_position_advice(
    packet: DeltaPacket,
    *,
    slot_id: ReviewSlotId,
    deterministic_action: ReviewAction,
    as_of: datetime | None = None,
) -> PositionAdvice:
    """Mirror the deterministic review into SHADOW PositionAdvice using the packet."""
    if packet.role is not DeskRole.POSITION:
        raise ValueError(f"expected POSITION packet, got {packet.role}")
    if packet.trade_id is None:
        raise ValueError("POSITION delta packet requires trade_id")
    agent_action = map_review_action_to_agent(deterministic_action)
    if agent_action not in ALLOWED_POSITION_ACTIONS:
        agent_action = AgentAction.ABSTAIN
    return PositionAdvice(
        as_of=as_of or packet.as_of,
        snapshot_id=packet.snapshot_id,
        trade_id=packet.trade_id,
        slot_id=slot_id,
        action=agent_action,
        mirrors_deterministic=deterministic_action,
        packet_version=packet.packet_version,
        evidence_ids=("delta-packet",),
        narrative=(
            "SHADOW mirror of deterministic review; no live influence on exits."
        ),
    )


def maybe_log_position_shadow(
    packet: DeltaPacket,
    *,
    slot_id: ReviewSlotId,
    deterministic_action: ReviewAction,
    decision_log: DecisionLog | None,
    enabled: bool,
    run_id: str,
    environment: Environment = Environment.PAPER,
) -> PositionShadowResult:
    """Review-slot hook: log SHADOW alongside deterministic; never change fills."""
    if not enabled:
        return PositionShadowResult(
            status="SKIPPED_DISABLED", advice=None, decision=None
        )
    advice = build_shadow_position_advice(
        packet, slot_id=slot_id, deterministic_action=deterministic_action
    )
    decision = AgentDecision(
        decision_id=f"DEC-POS-{uuid4().hex[:12]}",
        run_id=run_id,
        role=DeskRole.POSITION,
        mode=AuthorityMode.SHADOW,
        environment=environment,
        trade_id=advice.trade_id,
        snapshot_id=advice.snapshot_id,
        action=advice.action,
        confidence=None,
        size_multiplier=None,
        deterministic_choice=deterministic_action.value,
        agent_override=False,
        reason_codes=("MIRROR_DETERMINISTIC",),
        ungrounded_codes=(),
        evidence_ids=advice.evidence_ids,
        gate_outcome=GateOutcome.SHADOW_ONLY,
        gate_reject_codes=None,
        model_id=POSITION_MODEL_ID,
        prompt_version=POSITION_PROMPT_VERSION,
        policy_version=POSITION_POLICY_VERSION,
        packet_version=advice.packet_version,
        input_tokens=0,
        output_tokens=0,
        latency_ms=0,
        created_at=advice.as_of,
    )
    if decision_log is not None:
        decision_log.record(decision)
    return PositionShadowResult(status="LOGGED", advice=advice, decision=decision)
