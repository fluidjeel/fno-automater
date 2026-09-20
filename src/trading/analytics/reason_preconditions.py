"""Deterministic reason-code preconditions (ADESK-A8 / PART 11.1).

Agents may only cite codes whose predicates pass against the same EvidenceBundle.
Any failure strips the code, downgrades the action to ABSTAIN, and yields a
HallucinationEvent payload for the caller to persist.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading.domain.contracts.hallucination import GroundingResult, HallucinationEvent
from trading.domain.enums import AgentAction, AgentReasonCode, DeskRole

__all__ = [
    "EvidenceBundle",
    "PRECONDITIONS",
    "ground_agent_reasons",
]

Predicate = Callable[["EvidenceBundle"], bool]


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """Snapshot facts a reason-code predicate may read. No prose fields."""

    iv_percentile_delta: Decimal = Decimal(0)
    trend_state: str = "FLAT"  # UP | DOWN | FLAT
    thesis_direction: str = "FLAT"
    spread_bps: Decimal = Decimal(0)
    event_blackout: bool = False
    structure_is_defined_risk: bool = True
    realized_vol_z: Decimal = Decimal(0)


def _vol_expansion(e: EvidenceBundle) -> bool:
    return e.iv_percentile_delta >= Decimal("10")


def _vol_compression(e: EvidenceBundle) -> bool:
    return e.iv_percentile_delta <= Decimal("-10")


def _regime_alignment(e: EvidenceBundle) -> bool:
    return e.trend_state == e.thesis_direction and e.trend_state != "FLAT"


def _trend_confirmation(e: EvidenceBundle) -> bool:
    return e.trend_state in {"UP", "DOWN"} and e.realized_vol_z > Decimal("-1")


def _mean_reversion(e: EvidenceBundle) -> bool:
    return abs(e.realized_vol_z) >= Decimal("1.5")


def _liquidity(e: EvidenceBundle) -> bool:
    return e.spread_bps <= Decimal("25")


def _event_clear(e: EvidenceBundle) -> bool:
    return not e.event_blackout


def _defined_risk(e: EvidenceBundle) -> bool:
    return e.structure_is_defined_risk


PRECONDITIONS: Mapping[AgentReasonCode, Predicate] = {
    AgentReasonCode.VOLATILITY_EXPANSION: _vol_expansion,
    AgentReasonCode.VOLATILITY_COMPRESSION: _vol_compression,
    AgentReasonCode.REGIME_ALIGNMENT: _regime_alignment,
    AgentReasonCode.TREND_CONFIRMATION: _trend_confirmation,
    AgentReasonCode.MEAN_REVERSION_SETUP: _mean_reversion,
    AgentReasonCode.LIQUIDITY_ADEQUATE: _liquidity,
    AgentReasonCode.EVENT_CLEAR: _event_clear,
    AgentReasonCode.STRUCTURE_DEFINED_RISK: _defined_risk,
}


def ground_agent_reasons(
    *,
    reason_codes: Sequence[AgentReasonCode | str],
    evidence: EvidenceBundle,
    action: AgentAction,
    decision_id: str,
    role: DeskRole,
    model_id: str,
    prompt_version: str,
    event_id: str,
    created_at: datetime,
) -> tuple[GroundingResult, HallucinationEvent | None]:
    """Validate cited reasons; downgrade to ABSTAIN when any are ungrounded."""
    grounded: list[AgentReasonCode] = []
    ungrounded: list[AgentReasonCode] = []
    for raw in reason_codes:
        code = raw if isinstance(raw, AgentReasonCode) else AgentReasonCode(raw)
        predicate = PRECONDITIONS.get(code)
        if predicate is None or not predicate(evidence):
            ungrounded.append(code)
        else:
            grounded.append(code)

    downgraded = len(ungrounded) > 0
    effective = AgentAction.ABSTAIN if downgraded else action
    result = GroundingResult(
        grounded_codes=tuple(grounded),
        ungrounded_codes=tuple(ungrounded),
        original_action=action,
        effective_action=effective,
        downgraded=downgraded,
    )
    event: HallucinationEvent | None = None
    if downgraded:
        event = HallucinationEvent(
            event_id=event_id,
            decision_id=decision_id,
            role=role,
            model_id=model_id,
            prompt_version=prompt_version,
            ungrounded_codes=tuple(ungrounded),
            original_action=action,
            effective_action=effective,
            created_at=created_at,
        )
    return result, event
