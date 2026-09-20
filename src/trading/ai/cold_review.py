"""Cold-review scheduler and warm/cold divergence (ADESK-B5).

Every Nth review (default 5) runs warm + cold paths. SHADOW logging only.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from trading.ai.decision_log import DecisionLog
from trading.ai.packets import DeltaPacket
from trading.ai.position import (
    build_shadow_position_advice,
)
from trading.domain.contracts.agent_decision import AgentDecision
from trading.domain.contracts.position_advice import PositionAdvice
from trading.domain.enums import (
    AuthorityMode,
    DeskRole,
    Environment,
    GateOutcome,
    ReviewAction,
    ReviewSlotId,
)

__all__ = [
    "DEFAULT_COLD_EVERY_N",
    "ColdReviewPair",
    "WarmColdDivergenceReport",
    "maybe_run_cold_review",
    "should_run_cold",
    "warm_cold_divergence",
]

DEFAULT_COLD_EVERY_N = 5


@dataclass(frozen=True, slots=True)
class ColdReviewPair:
    """Warm + cold shadow advice for one scheduled dual-path review."""

    review_index: int
    warm: PositionAdvice
    cold: PositionAdvice
    diverged: bool


@dataclass(frozen=True, slots=True)
class WarmColdDivergenceReport:
    pairs: int
    divergences: int
    warm_cold_divergence: Decimal


def should_run_cold(review_index: int, *, every_n: int = DEFAULT_COLD_EVERY_N) -> bool:
    """True on every Nth review (1-based: 5, 10, ...)."""
    if every_n <= 0:
        return False
    return review_index > 0 and review_index % every_n == 0


def warm_cold_divergence(pairs: tuple[ColdReviewPair, ...]) -> WarmColdDivergenceReport:
    """Fraction of dual-path reviews where warm and cold actions differ."""
    if not pairs:
        return WarmColdDivergenceReport(
            pairs=0, divergences=0, warm_cold_divergence=Decimal("0")
        )
    divergences = sum(1 for p in pairs if p.diverged)
    rate = (Decimal(divergences) / Decimal(len(pairs))).quantize(Decimal("0.0001"))
    return WarmColdDivergenceReport(
        pairs=len(pairs), divergences=divergences, warm_cold_divergence=rate
    )


def maybe_run_cold_review(
    packet: DeltaPacket,
    *,
    review_index: int,
    slot_id: ReviewSlotId,
    warm_deterministic: ReviewAction,
    cold_deterministic: ReviewAction,
    decision_log: DecisionLog | None,
    enabled: bool,
    run_id: str,
    every_n: int = DEFAULT_COLD_EVERY_N,
    environment: Environment = Environment.PAPER,
) -> ColdReviewPair | None:
    """When due, log warm+cold SHADOW decisions; return None when skipped."""
    if not enabled or not should_run_cold(review_index, every_n=every_n):
        return None
    warm = build_shadow_position_advice(
        packet, slot_id=slot_id, deterministic_action=warm_deterministic
    )
    # Cold path: same packet but without prior narrative extras.
    cold_packet = packet.model_copy(
        update={"question": "Cold review: entry conditions vs state only."}
    )
    cold = build_shadow_position_advice(
        cold_packet, slot_id=slot_id, deterministic_action=cold_deterministic
    )
    diverged = warm.action != cold.action
    if decision_log is not None:
        for tag, advice, det in (
            ("WARM", warm, warm_deterministic),
            ("COLD", cold, cold_deterministic),
        ):
            decision_log.record(
                AgentDecision(
                    decision_id=f"DEC-{tag}-{uuid4().hex[:10]}",
                    run_id=run_id,
                    role=DeskRole.POSITION,
                    mode=AuthorityMode.SHADOW,
                    environment=environment,
                    trade_id=advice.trade_id,
                    snapshot_id=advice.snapshot_id,
                    action=advice.action,
                    confidence=None,
                    size_multiplier=None,
                    deterministic_choice=f"{tag}:{det.value}",
                    agent_override=False,
                    reason_codes=(tag,),
                    ungrounded_codes=(),
                    evidence_ids=advice.evidence_ids,
                    gate_outcome=GateOutcome.SHADOW_ONLY,
                    gate_reject_codes=None,
                    model_id="deterministic-shadow",
                    prompt_version="cold-review-v1",
                    policy_version="position-policy-v1",
                    packet_version=advice.packet_version,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=0,
                    created_at=advice.as_of,
                )
            )
    return ColdReviewPair(
        review_index=review_index, warm=warm, cold=cold, diverged=diverged
    )
