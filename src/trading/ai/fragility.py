"""FRAGILITY desk ADVISORY narration over StressReport (ADESK-B9)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import uuid4

from trading.ai.decision_log import DecisionLog
from trading.domain.contracts.agent_decision import AgentDecision
from trading.domain.contracts.stress import StressReport
from trading.domain.enums import (
    AgentAction,
    AuthorityMode,
    DeskRole,
    Environment,
    GateOutcome,
)

__all__ = [
    "FragilityNarration",
    "FragilityResult",
    "build_fragility_telegram_line",
    "maybe_log_fragility",
]


@dataclass(frozen=True, slots=True)
class FragilityNarration:
    as_of: datetime
    snapshot_id: str
    worst_case_amount: str
    worst_case_currency: str
    contributor_ids: tuple[str, ...]
    telegram_line: str


@dataclass(frozen=True, slots=True)
class FragilityResult:
    status: Literal["LOGGED", "SKIPPED_DISABLED"]
    narration: FragilityNarration | None
    decision: AgentDecision | None


def build_fragility_telegram_line(report: StressReport) -> FragilityNarration:
    """Daily fragility line (string builder only — does not send)."""
    contributors = tuple(sorted({flag.value for flag in report.fragility_flags})) or (
        "NONE",
    )
    line = (
        f"FRAGILITY {report.snapshot_id}: worst_case="
        f"{report.worst_case.amount} {report.worst_case.currency.value} "
        f"({report.worst_case_pct_equity} of equity); "
        f"flags={','.join(contributors)}; breached={report.breached_budget}"
    )
    return FragilityNarration(
        as_of=report.as_of,
        snapshot_id=report.snapshot_id,
        worst_case_amount=str(report.worst_case.amount),
        worst_case_currency=report.worst_case.currency.value,
        contributor_ids=contributors,
        telegram_line=line,
    )


def maybe_log_fragility(
    report: StressReport,
    *,
    decision_log: DecisionLog | None,
    enabled: bool,
    run_id: str,
    environment: Environment = Environment.PAPER,
) -> FragilityResult:
    """ADVISORY narration only; never freezes or unfreezes entries itself."""
    if not enabled:
        return FragilityResult(status="SKIPPED_DISABLED", narration=None, decision=None)
    narration = build_fragility_telegram_line(report)
    decision = AgentDecision(
        decision_id=f"DEC-FR-{uuid4().hex[:12]}",
        run_id=run_id,
        role=DeskRole.FRAGILITY,
        mode=AuthorityMode.ADVISORY,
        environment=environment,
        trade_id=None,
        snapshot_id=report.snapshot_id,
        action=AgentAction.REQUEST_OPERATOR_ATTENTION
        if report.breached_budget
        else AgentAction.HOLD,
        confidence=None,
        size_multiplier=None,
        deterministic_choice=str(report.worst_case.amount),
        agent_override=False,
        reason_codes=narration.contributor_ids,
        ungrounded_codes=(),
        evidence_ids=(report.snapshot_id,),
        gate_outcome=GateOutcome.SHADOW_ONLY,
        gate_reject_codes=None,
        model_id="deterministic-shadow",
        prompt_version="fragility-v1",
        policy_version="fragility-policy-v1",
        packet_version="fragility-packet-v1",
        input_tokens=0,
        output_tokens=0,
        latency_ms=0,
        created_at=report.as_of,
    )
    if decision_log is not None:
        decision_log.record(decision)
    return FragilityResult(status="LOGGED", narration=narration, decision=decision)
