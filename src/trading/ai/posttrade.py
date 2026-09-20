"""POSTTRADE desk ADVISORY attribution (ADESK-B8)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from trading.ai.decision_log import DecisionLog
from trading.domain.contracts.agent_decision import AgentDecision
from trading.domain.contracts.posttrade import (
    TradeAttribution,
    entry_journal_hash,
    verify_entry_journal_hash,
)
from trading.domain.enums import (
    AgentAction,
    AttributionCode,
    AuthorityMode,
    DeskRole,
    Environment,
    GateOutcome,
    ThesisVerdict,
)

__all__ = ["PosttradeResult", "build_trade_attribution", "maybe_log_posttrade"]


@dataclass(frozen=True, slots=True)
class PosttradeResult:
    status: Literal["LOGGED", "SKIPPED_DISABLED", "HASH_MISMATCH"]
    attribution: TradeAttribution | None
    decision: AgentDecision | None


def _verdict(*, outcome_r: Decimal, thesis_correct: bool) -> ThesisVerdict:
    if thesis_correct and outcome_r > 0:
        return ThesisVerdict.CORRECT_AND_PAID
    if thesis_correct and outcome_r <= 0:
        return ThesisVerdict.CORRECT_UNPAID
    if not thesis_correct and outcome_r < 0:
        return ThesisVerdict.WRONG_AND_LOST
    if not thesis_correct and outcome_r >= 0:
        return ThesisVerdict.WRONG_BUT_PAID
    return ThesisVerdict.UNTESTED


def build_trade_attribution(
    *,
    trade_id: str,
    thesis_id: str,
    thesis_hash: str,
    entry_narrative: str,
    stored_entry_hash: str,
    outcome_r: Decimal,
    mae_r: Decimal,
    mfe_r: Decimal,
    thesis_correct: bool,
    execution_cost_r: Decimal = Decimal("0"),
) -> TradeAttribution:
    verified = verify_entry_journal_hash(
        stored_hash=stored_entry_hash,
        thesis_hash=thesis_hash,
        narrative=entry_narrative,
    )
    if not verified:
        raise ValueError("entry journal hash mismatch")
    capture = (
        Decimal("0") if mfe_r == 0 else (outcome_r / mfe_r).quantize(Decimal("0.0001"))
    )
    return TradeAttribution(
        trade_id=trade_id,
        thesis_id=thesis_id,
        thesis_hash_verified=True,
        outcome_r=outcome_r,
        mae_r=mae_r,
        mfe_r=mfe_r,
        capture_ratio=capture,
        thesis_verdict=_verdict(outcome_r=outcome_r, thesis_correct=thesis_correct),
        invalidation_fired=(),
        primary_attribution=AttributionCode.DIRECTION,
        execution_cost_r=execution_cost_r,
        improvement_records=(),
        narrative="POSTTRADE dual-entry attribution",
        entry_journal_hash=stored_entry_hash,
    )


def maybe_log_posttrade(
    *,
    as_of: datetime,
    attribution: TradeAttribution,
    decision_log: DecisionLog | None,
    enabled: bool,
    run_id: str,
    snapshot_id: str,
    environment: Environment = Environment.PAPER,
) -> PosttradeResult:
    if not enabled:
        return PosttradeResult(
            status="SKIPPED_DISABLED", attribution=None, decision=None
        )
    decision = AgentDecision(
        decision_id=f"DEC-PT-{uuid4().hex[:12]}",
        run_id=run_id,
        role=DeskRole.POSTTRADE,
        mode=AuthorityMode.ADVISORY,
        environment=environment,
        trade_id=attribution.trade_id,
        snapshot_id=snapshot_id,
        action=AgentAction.RECORD_IMPROVEMENT,
        confidence=None,
        size_multiplier=None,
        deterministic_choice=attribution.thesis_verdict.value,
        agent_override=False,
        reason_codes=(attribution.primary_attribution.value,),
        ungrounded_codes=(),
        evidence_ids=(attribution.thesis_id,),
        gate_outcome=GateOutcome.SHADOW_ONLY,
        gate_reject_codes=None,
        model_id="deterministic-shadow",
        prompt_version="posttrade-v1",
        policy_version="posttrade-policy-v1",
        packet_version="posttrade-packet-v1",
        input_tokens=0,
        output_tokens=0,
        latency_ms=0,
        created_at=as_of,
    )
    if decision_log is not None:
        decision_log.record(decision)
    return PosttradeResult(status="LOGGED", attribution=attribution, decision=decision)


# re-export helper for callers writing the entry journal
hash_entry_journal = entry_journal_hash
