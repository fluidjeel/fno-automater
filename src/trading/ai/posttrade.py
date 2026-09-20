"""POSTTRADE desk ADVISORY attribution (ADESK-B8 / ADESK-D2).

ADVISORY requires a signed AuthorityGrant; missing/invalid grant demotes to
OBSERVE. Advisory line builder is string-only — does not send.
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

__all__ = [
    "POSTTRADE_MODEL_ID",
    "POSTTRADE_PACKET_VERSION",
    "POSTTRADE_POLICY_VERSION",
    "POSTTRADE_PROMPT_VERSION",
    "PosttradeAdvisoryLine",
    "PosttradeResult",
    "build_posttrade_advisory_line",
    "build_trade_attribution",
    "maybe_log_posttrade",
]

POSTTRADE_MODEL_ID = "deterministic-shadow"
POSTTRADE_PROMPT_VERSION = "posttrade-v1"
POSTTRADE_POLICY_VERSION = "posttrade-policy-v1"
POSTTRADE_PACKET_VERSION = "posttrade-packet-v1"


@dataclass(frozen=True, slots=True)
class PosttradeAdvisoryLine:
    """Operator-facing string payload. Builder only — never sends."""

    trade_id: str
    thesis_verdict: str
    primary_attribution: str
    line: str


@dataclass(frozen=True, slots=True)
class PosttradeResult:
    status: Literal["LOGGED", "SKIPPED_DISABLED", "HASH_MISMATCH", "OBSERVE"]
    attribution: TradeAttribution | None
    decision: AgentDecision | None
    mode: AuthorityMode
    advisory: PosttradeAdvisoryLine | None = None


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


def build_posttrade_advisory_line(
    attribution: TradeAttribution,
) -> PosttradeAdvisoryLine:
    """Advisory/Telegram-shaped line (string builder only — does not send)."""
    line = (
        f"POSTTRADE {attribution.trade_id}: verdict="
        f"{attribution.thesis_verdict.value}; "
        f"attr={attribution.primary_attribution.value}; "
        f"outcome_r={attribution.outcome_r}; "
        f"capture={attribution.capture_ratio}"
    )
    return PosttradeAdvisoryLine(
        trade_id=attribution.trade_id,
        thesis_verdict=attribution.thesis_verdict.value,
        primary_attribution=attribution.primary_attribution.value,
        line=line,
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
    grant: AuthorityGrant | None = None,
    now: datetime | None = None,
) -> PosttradeResult:
    if not enabled:
        return PosttradeResult(
            status="SKIPPED_DISABLED",
            attribution=None,
            decision=None,
            mode=AuthorityMode.OBSERVE,
            advisory=None,
        )
    mode = resolve_effective_mode(
        DeskRole.POSTTRADE,
        runtime_model_id=POSTTRADE_MODEL_ID,
        runtime_prompt_version=POSTTRADE_PROMPT_VERSION,
        runtime_policy_version=POSTTRADE_POLICY_VERSION,
        now=now or as_of,
        grant=grant,
    )
    advisory = build_posttrade_advisory_line(attribution)
    decision = AgentDecision(
        decision_id=f"DEC-PT-{uuid4().hex[:12]}",
        run_id=run_id,
        role=DeskRole.POSTTRADE,
        mode=mode,
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
        model_id=POSTTRADE_MODEL_ID,
        prompt_version=POSTTRADE_PROMPT_VERSION,
        policy_version=POSTTRADE_POLICY_VERSION,
        packet_version=POSTTRADE_PACKET_VERSION,
        input_tokens=0,
        output_tokens=0,
        latency_ms=0,
        created_at=as_of,
    )
    if decision_log is not None:
        decision_log.record(decision)
    status: Literal["LOGGED", "OBSERVE"] = (
        "OBSERVE" if mode is AuthorityMode.OBSERVE else "LOGGED"
    )
    return PosttradeResult(
        status=status,
        attribution=attribution,
        decision=decision,
        mode=mode,
        advisory=advisory,
    )


# re-export helper for callers writing the entry journal
hash_entry_journal = entry_journal_hash
