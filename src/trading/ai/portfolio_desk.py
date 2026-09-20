"""PORTFOLIO desk SHADOW + SAME_THESIS_DRIVER recall (ADESK-B6)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from trading.ai.decision_log import DecisionLog
from trading.domain.contracts.agent_decision import AgentDecision
from trading.domain.contracts.portfolio_desk import PortfolioVeto, SharedFateAssessment
from trading.domain.contracts.trade_thesis import TradeThesis
from trading.domain.enums import (
    AgentAction,
    AuthorityMode,
    DeskRole,
    Environment,
    GateOutcome,
    Severity,
    SharedFateCode,
)

__all__ = [
    "PortfolioShadowResult",
    "SameThesisDriverRecall",
    "deterministic_same_thesis_pairs",
    "maybe_log_portfolio_shadow",
    "same_thesis_driver_recall",
]


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
        model_id="deterministic-shadow",
        prompt_version="portfolio-shadow-v1",
        policy_version="portfolio-policy-v1",
        packet_version="portfolio-packet-v1",
        input_tokens=0,
        output_tokens=0,
        latency_ms=0,
        created_at=as_of,
    )
    if decision_log is not None:
        decision_log.record(decision)
    return PortfolioShadowResult(status="LOGGED", veto=veto, decision=decision)
