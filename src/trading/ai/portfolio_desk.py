"""PORTFOLIO desk SHADOW + config-promotion BOUNDED (ADESK-B6 / ADESK-D3).

C1: BOUNDED attaches only to PROPOSE_* FamilyStance actions. Live-path
VETO_ENTRY / approve-as-order never take BOUNDED grants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from trading.ai.authority import resolve_effective_mode
from trading.ai.decision_log import DecisionLog
from trading.domain.contracts.agent_decision import AgentDecision
from trading.domain.contracts.authority import AuthorityGrant
from trading.domain.contracts.portfolio_desk import PortfolioVeto, SharedFateAssessment
from trading.domain.contracts.trade_thesis import TradeThesis
from trading.domain.enums import (
    BOUNDED_ACTIONS,
    AgentAction,
    AuthorityMode,
    DeskRole,
    Environment,
    GateOutcome,
    Severity,
    SharedFateCode,
)

__all__ = [
    "PortfolioConfigPromotionResult",
    "PortfolioShadowResult",
    "SameThesisDriverRecall",
    "deterministic_same_thesis_pairs",
    "maybe_log_portfolio_config_promotion",
    "maybe_log_portfolio_shadow",
    "portfolio_bounded_actions_allowed",
    "same_thesis_driver_recall",
]

PORTFOLIO_MODEL_ID = "deterministic-shadow"
PORTFOLIO_PROMPT_VERSION = "portfolio-shadow-v1"
PORTFOLIO_POLICY_VERSION = "portfolio-policy-v1"
PORTFOLIO_PACKET_VERSION = "portfolio-packet-v1"
PORTFOLIO_CONFIG_PROMPT_VERSION = "portfolio-config-v1"
PORTFOLIO_CONFIG_POLICY_VERSION = "portfolio-config-policy-v1"
PORTFOLIO_CONFIG_PACKET_VERSION = "portfolio-config-packet-v1"


@dataclass(frozen=True, slots=True)
class SameThesisDriverRecall:
    ground_truth_pairs: int
    flagged_pairs: int
    recall: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioShadowResult:
    status: Literal["LOGGED", "SKIPPED_DISABLED"]
    veto: PortfolioVeto | None
    decision: AgentDecision | None


@dataclass(frozen=True, slots=True)
class PortfolioConfigPromotionResult:
    status: Literal["LOGGED", "SKIPPED_DISABLED", "OBSERVE", "REJECTED_NOT_BOUNDED"]
    decision: AgentDecision | None
    mode: AuthorityMode


def deterministic_same_thesis_pairs(
    theses: tuple[TradeThesis, ...],
) -> frozenset[tuple[str, str]]:
    """Ground-truth pairs sharing primary_driver (unordered id pairs)."""
    pairs: set[tuple[str, str]] = set()
    for i, a in enumerate(theses):
        for b in theses[i + 1 :]:
            if a.primary_driver is b.primary_driver:
                ordered = tuple(sorted((a.trade_id, b.trade_id)))
                pairs.add((ordered[0], ordered[1]))
    return frozenset(pairs)


def same_thesis_driver_recall(
    *,
    ground_truth: frozenset[tuple[str, str]],
    agent_flagged: frozenset[tuple[str, str]],
) -> SameThesisDriverRecall:
    """Recall of agent SAME_THESIS_DRIVER flags vs deterministic ground truth."""
    if not ground_truth:
        return SameThesisDriverRecall(
            ground_truth_pairs=0, flagged_pairs=0, recall=Decimal("1")
        )
    flagged = len(ground_truth & agent_flagged)
    recall = (Decimal(flagged) / Decimal(len(ground_truth))).quantize(Decimal("0.0001"))
    return SameThesisDriverRecall(
        ground_truth_pairs=len(ground_truth),
        flagged_pairs=flagged,
        recall=recall,
    )


def maybe_log_portfolio_shadow(
    *,
    as_of: datetime,
    candidate_trade_id: str,
    open_theses: tuple[TradeThesis, ...],
    candidate_thesis: TradeThesis | None,
    decision_log: DecisionLog | None,
    enabled: bool,
    run_id: str,
    snapshot_id: str,
    environment: Environment = Environment.PAPER,
) -> PortfolioShadowResult:
    """SHADOW: deterministically flag SAME_THESIS_DRIVER; log only."""
    if not enabled:
        return PortfolioShadowResult(
            status="SKIPPED_DISABLED", veto=None, decision=None
        )
    assessments: list[SharedFateAssessment] = []
    if candidate_thesis is not None:
        for thesis in open_theses:
            if thesis.primary_driver is candidate_thesis.primary_driver:
                assessments.append(
                    SharedFateAssessment(
                        existing_trade_id=thesis.trade_id,
                        code=SharedFateCode.SAME_THESIS_DRIVER,
                        severity=Severity.WARNING,
                        evidence_id=thesis.thesis_id,
                    )
                )
    action = AgentAction.HOLD
    if assessments:
        action = AgentAction.VETO_ENTRY
    veto = PortfolioVeto(
        as_of=as_of,
        candidate_trade_id=candidate_trade_id,
        action=action,
        shared_fate=tuple(assessments),
        size_multiplier=Decimal("1"),
        evidence_ids=tuple(a.evidence_id for a in assessments),
        narrative="SHADOW shared-fate scan; veto-only; no live influence.",
    )
    decision = AgentDecision(
        decision_id=f"DEC-PF-{uuid4().hex[:12]}",
        run_id=run_id,
        role=DeskRole.PORTFOLIO,
        mode=AuthorityMode.SHADOW,
        environment=environment,
        trade_id=candidate_trade_id,
        snapshot_id=snapshot_id,
        action=veto.action,
        confidence=None,
        size_multiplier=veto.size_multiplier,
        deterministic_choice=SharedFateCode.SAME_THESIS_DRIVER.value,
        agent_override=False,
        reason_codes=(SharedFateCode.SAME_THESIS_DRIVER.value,)
        if assessments
        else (SharedFateCode.NO_SHARED_FATE.value,),
        ungrounded_codes=(),
        evidence_ids=veto.evidence_ids,
        gate_outcome=GateOutcome.SHADOW_ONLY,
        gate_reject_codes=None,
        model_id=PORTFOLIO_MODEL_ID,
        prompt_version=PORTFOLIO_PROMPT_VERSION,
        policy_version=PORTFOLIO_POLICY_VERSION,
        packet_version=PORTFOLIO_PACKET_VERSION,
        input_tokens=0,
        output_tokens=0,
        latency_ms=0,
        created_at=as_of,
    )
    if decision_log is not None:
        decision_log.record(decision)
    return PortfolioShadowResult(status="LOGGED", veto=veto, decision=decision)


def portfolio_bounded_actions_allowed(
    actions: tuple[AgentAction, ...],
) -> bool:
    """True iff every action is config-promotion (BOUNDED-eligible under C1)."""
    return bool(actions) and all(a in BOUNDED_ACTIONS for a in actions)


def maybe_log_portfolio_config_promotion(
    *,
    as_of: datetime,
    action: AgentAction,
    strategy_family: str,
    decision_log: DecisionLog | None,
    enabled: bool,
    run_id: str,
    snapshot_id: str,
    environment: Environment = Environment.PAPER,
    grant: AuthorityGrant | None = None,
    now: datetime | None = None,
) -> PortfolioConfigPromotionResult:
    """BOUNDED path for PROPOSE_* FamilyStance only. No veto/approve-as-order.

    Live-path actions cannot use this path: grant validation rejects
    BOUNDED+live-path, and this helper refuses non-BOUNDED_ACTIONS.
    """
    if not enabled:
        return PortfolioConfigPromotionResult(
            status="SKIPPED_DISABLED",
            decision=None,
            mode=AuthorityMode.OBSERVE,
        )
    if action not in BOUNDED_ACTIONS:
        return PortfolioConfigPromotionResult(
            status="REJECTED_NOT_BOUNDED",
            decision=None,
            mode=AuthorityMode.OBSERVE,
        )
    mode = resolve_effective_mode(
        DeskRole.PORTFOLIO,
        runtime_model_id=PORTFOLIO_MODEL_ID,
        runtime_prompt_version=PORTFOLIO_CONFIG_PROMPT_VERSION,
        runtime_policy_version=PORTFOLIO_CONFIG_POLICY_VERSION,
        now=now or as_of,
        grant=grant,
    )
    if mode is not AuthorityMode.BOUNDED or grant is None:
        return PortfolioConfigPromotionResult(
            status="OBSERVE", decision=None, mode=mode
        )
    if action not in grant.allowed_actions:
        return PortfolioConfigPromotionResult(
            status="REJECTED_NOT_BOUNDED",
            decision=None,
            mode=mode,
        )
    if strategy_family not in grant.strategy_families:
        return PortfolioConfigPromotionResult(
            status="REJECTED_NOT_BOUNDED",
            decision=None,
            mode=mode,
        )
    decision = AgentDecision(
        decision_id=f"DEC-PF-CFG-{uuid4().hex[:12]}",
        run_id=run_id,
        role=DeskRole.PORTFOLIO,
        mode=AuthorityMode.BOUNDED,
        environment=environment,
        trade_id=None,
        snapshot_id=snapshot_id,
        action=action,
        confidence=None,
        size_multiplier=None,
        deterministic_choice=strategy_family,
        agent_override=False,
        reason_codes=(action.value,),
        ungrounded_codes=(),
        evidence_ids=(grant.evidence_report_id,),
        gate_outcome=GateOutcome.ACCEPTED,
        gate_reject_codes=None,
        model_id=PORTFOLIO_MODEL_ID,
        prompt_version=PORTFOLIO_CONFIG_PROMPT_VERSION,
        policy_version=PORTFOLIO_CONFIG_POLICY_VERSION,
        packet_version=PORTFOLIO_CONFIG_PACKET_VERSION,
        input_tokens=0,
        output_tokens=0,
        latency_ms=0,
        created_at=as_of,
    )
    if decision_log is not None:
        decision_log.record(decision)
    return PortfolioConfigPromotionResult(
        status="LOGGED", decision=decision, mode=AuthorityMode.BOUNDED
    )
