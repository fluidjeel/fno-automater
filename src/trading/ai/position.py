"""POSITION desk SHADOW/ADVISORY runner (ADESK-B3 / ADESK-D4).

Logs advice from a DeltaPacket alongside the deterministic review.
Never influences stops, exits, or OMS. No LLM on the intraday path.

C1 / D4: TIGHTEN_STOP and PARTIAL_EXIT may run at SHADOW or ADVISORY only —
BOUNDED grants for those live-path actions are refused at grant validate and
at desk resolve.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import uuid4

from trading.ai.authority import resolve_effective_mode
from trading.ai.decision_log import DecisionLog
from trading.ai.packets import DeltaPacket
from trading.domain.contracts.agent_decision import AgentDecision
from trading.domain.contracts.authority import AuthorityGrant
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
    "POSITION_LIVE_PATH_ACTIONS",
    "POSITION_MODEL_ID",
    "POSITION_PACKET_VERSION",
    "POSITION_POLICY_VERSION",
    "POSITION_PROMPT_VERSION",
    "PositionShadowResult",
    "build_shadow_position_advice",
    "map_review_action_to_agent",
    "maybe_log_position_shadow",
    "position_mode_for_grant",
    "refuse_bounded_position_live_path",
]

POSITION_PROMPT_VERSION = "position-shadow-v1"
POSITION_PACKET_VERSION = "position-packet-v1"
POSITION_POLICY_VERSION = "position-policy-v1"
POSITION_MODEL_ID = "deterministic-shadow"

POSITION_LIVE_PATH_ACTIONS: frozenset[AgentAction] = frozenset(
    {
        AgentAction.TIGHTEN_STOP,
        AgentAction.PARTIAL_EXIT,
        AgentAction.FULL_EXIT,
    }
)


@dataclass(frozen=True, slots=True)
class PositionShadowResult:
    status: Literal["LOGGED", "SKIPPED_DISABLED", "REJECTED_BOUNDED"]
    advice: PositionAdvice | None
    decision: AgentDecision | None
    mode: AuthorityMode


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


def refuse_bounded_position_live_path(mode: AuthorityMode, action: AgentAction) -> bool:
    """True when a live-path POSITION action is illegally paired with BOUNDED."""
    return mode is AuthorityMode.BOUNDED and action in POSITION_LIVE_PATH_ACTIONS


def position_mode_for_grant(
    grant: AuthorityGrant | None,
    *,
    now: datetime,
) -> AuthorityMode:
    """Resolve effective mode; POSITION never elevates live-path to BOUNDED."""
    return resolve_effective_mode(
        DeskRole.POSITION,
        runtime_model_id=POSITION_MODEL_ID,
        runtime_prompt_version=POSITION_PROMPT_VERSION,
        runtime_policy_version=POSITION_POLICY_VERSION,
        now=now,
        grant=grant,
    )


def build_shadow_position_advice(
    packet: DeltaPacket,
    *,
    slot_id: ReviewSlotId,
    deterministic_action: ReviewAction,
    as_of: datetime | None = None,
) -> PositionAdvice:
    """Mirror the deterministic review into PositionAdvice using the packet."""
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
            "POSITION desk mirror of deterministic review; SHADOW/ADVISORY only; "
            "no live BOUNDED influence on exits."
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
    grant: AuthorityGrant | None = None,
    now: datetime | None = None,
) -> PositionShadowResult:
    """Review-slot hook: log SHADOW/ADVISORY alongside deterministic; never fills.

    BOUNDED is refused for TIGHTEN_STOP / PARTIAL_EXIT / FULL_EXIT even if a
    grant somehow claimed that mode (grant validate already blocks it under C1).
    """
    if not enabled:
        return PositionShadowResult(
            status="SKIPPED_DISABLED",
            advice=None,
            decision=None,
            mode=AuthorityMode.OBSERVE,
        )
    as_of = now or packet.as_of
    mode = position_mode_for_grant(grant, now=as_of)
    # Default shadow path when no grant: keep Stage B behaviour as SHADOW.
    if grant is None:
        mode = AuthorityMode.SHADOW
    advice = build_shadow_position_advice(
        packet, slot_id=slot_id, deterministic_action=deterministic_action
    )
    if refuse_bounded_position_live_path(mode, advice.action):
        return PositionShadowResult(
            status="REJECTED_BOUNDED",
            advice=advice,
            decision=None,
            mode=mode,
        )
    # Cap live-path at ADVISORY even if grant said BOUNDED (defence in depth).
    if advice.action in POSITION_LIVE_PATH_ACTIONS and mode is AuthorityMode.BOUNDED:
        mode = AuthorityMode.ADVISORY
    decision = AgentDecision(
        decision_id=f"DEC-POS-{uuid4().hex[:12]}",
        run_id=run_id,
        role=DeskRole.POSITION,
        mode=mode,
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
    return PositionShadowResult(
        status="LOGGED", advice=advice, decision=decision, mode=mode
    )
