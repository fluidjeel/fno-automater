"""ENTRY desk SHADOW runner (ADESK-B2).

Deterministic shadow advice + DecisionLog only. No LLM on the intraday path.
Does not mutate OMS, broker, gateway decisions, or paper fill outcomes.
Paper (or tests) call `maybe_log_entry_shadow` as the clear hook.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from trading.ai.decision_log import DecisionLog
from trading.domain.contracts.agent_decision import AgentDecision
from trading.domain.contracts.entry import (
    EntryAdvice,
    StrikeShortlist,
    assert_candidate_on_shortlist,
)
from trading.domain.contracts.identification import ConfidenceKind
from trading.domain.contracts.trade_thesis import InvalidationCondition, TradeThesis
from trading.domain.enums import (
    AgentAction,
    AuthorityMode,
    Comparator,
    DeskRole,
    DirectionalClaim,
    DriverCode,
    EntryGateId,
    Environment,
    GateOutcome,
    InvalidationMetric,
    InvalidationSeverity,
)

__all__ = [
    "ENTRY_PACKET_VERSION",
    "ENTRY_PROMPT_VERSION",
    "EntryShadowResult",
    "build_shadow_entry_advice",
    "maybe_log_entry_shadow",
    "shadow_decision_from_advice",
]

ENTRY_PROMPT_VERSION = "entry-shadow-v1"
ENTRY_PACKET_VERSION = "entry-packet-v1"
ENTRY_POLICY_VERSION = "entry-policy-v1"
ENTRY_MODEL_ID = "deterministic-shadow"


@dataclass(frozen=True, slots=True)
class EntryShadowResult:
    """Outcome of a SHADOW ENTRY hook. Never an order instruction."""

    status: Literal["LOGGED", "SKIPPED_DISABLED", "SKIPPED_SHORTLIST", "PASS"]
    advice: EntryAdvice | None
    decision: AgentDecision | None


def build_shadow_entry_advice(
    shortlist: StrikeShortlist,
    *,
    as_of: datetime,
    trade_id: str = "SHADOW-TRADE",
) -> EntryAdvice | None:
    """Build deterministic SELECT_STRIKE_CANDIDATE for the top-scored candidate.

    Returns None when shortlist has fewer than two candidates (skip agent / PASS).
    """
    if not shortlist.eligible_for_agent:
        return None
    top = shortlist.top_by_score()
    if top is None:
        return None
    thesis = _stub_thesis(
        trade_id=trade_id,
        snapshot_id=shortlist.snapshot_id,
        written_at=as_of,
    )
    advice = EntryAdvice(
        as_of=as_of,
        snapshot_id=shortlist.snapshot_id,
        action=AgentAction.SELECT_STRIKE_CANDIDATE,
        candidate_id=top.candidate_id,
        size_multiplier=Decimal("1"),
        thesis=thesis,
        evidence_ids=("shortlist-deterministic",),
        failed_gate_ids=(),
        narrative="SHADOW deterministic top-of-shortlist; no live influence.",
        agent_override=False,
    )
    assert_candidate_on_shortlist(advice, shortlist)
    return advice


def shadow_decision_from_advice(
    advice: EntryAdvice,
    *,
    run_id: str,
    decision_id: str | None = None,
    trade_id: str | None = None,
    environment: Environment = Environment.PAPER,
) -> AgentDecision:
    """Map EntryAdvice onto AgentDecision with gate_outcome SHADOW_ONLY."""
    return AgentDecision(
        decision_id=decision_id or f"DEC-ENTRY-{uuid4().hex[:12]}",
        run_id=run_id,
        role=DeskRole.ENTRY,
        mode=AuthorityMode.SHADOW,
        environment=environment,
        trade_id=trade_id,
        snapshot_id=advice.snapshot_id,
        action=advice.action,
        confidence=advice.thesis.confidence if advice.thesis is not None else None,
        size_multiplier=advice.size_multiplier,
        deterministic_choice=advice.candidate_id,
        agent_override=advice.agent_override,
        reason_codes=tuple(c.value for c in advice.veto_codes)
        if advice.veto_codes
        else ("DETERMINISTIC_TOP",),
        ungrounded_codes=(),
        evidence_ids=advice.evidence_ids,
        gate_outcome=GateOutcome.SHADOW_ONLY,
        gate_reject_codes=None,
        model_id=ENTRY_MODEL_ID,
        prompt_version=ENTRY_PROMPT_VERSION,
        policy_version=ENTRY_POLICY_VERSION,
        packet_version=ENTRY_PACKET_VERSION,
        input_tokens=0,
        output_tokens=0,
        latency_ms=0,
        created_at=advice.as_of,
    )


def maybe_log_entry_shadow(
    shortlist: StrikeShortlist,
    *,
    as_of: datetime,
    decision_log: DecisionLog | None,
    enabled: bool,
    run_id: str,
    trade_id: str | None = None,
    environment: Environment = Environment.PAPER,
) -> EntryShadowResult:
    """Paper-entry hook: log SHADOW EntryAdvice when enabled; never affect fills.

    When enabled is False the live/paper path is unchanged (no log, no advice).
    When shortlist has < 2 candidates, skip the agent (PASS / deterministic top
    is caller's job); no decision is written.
    """
    if not enabled:
        return EntryShadowResult(status="SKIPPED_DISABLED", advice=None, decision=None)
    if not shortlist.eligible_for_agent:
        return EntryShadowResult(status="SKIPPED_SHORTLIST", advice=None, decision=None)
    advice = build_shadow_entry_advice(
        shortlist, as_of=as_of, trade_id=trade_id or "SHADOW-TRADE"
    )
    if advice is None:
        return EntryShadowResult(status="PASS", advice=None, decision=None)
    decision = shadow_decision_from_advice(
        advice,
        run_id=run_id,
        trade_id=trade_id,
        environment=environment,
    )
    if decision_log is not None:
        decision_log.record(decision)
    return EntryShadowResult(status="LOGGED", advice=advice, decision=decision)


def _stub_thesis(
    *, trade_id: str, snapshot_id: str, written_at: datetime
) -> TradeThesis:
    """Minimal falsifiable thesis for SHADOW logging without an LLM."""
    inv_a = InvalidationCondition(
        condition_id="inv-spot-soft",
        metric=InvalidationMetric.SPOT_PCT_FROM_ENTRY,
        comparator=Comparator.LT,
        threshold=Decimal("-2"),
        window="2_sessions",
        severity=InvalidationSeverity.SOFT,
    )
    inv_b = InvalidationCondition(
        condition_id="inv-dte-hard",
        metric=InvalidationMetric.DTE,
        comparator=Comparator.LTE,
        threshold=Decimal("1"),
        window=None,
        severity=InvalidationSeverity.HARD,
    )
    return TradeThesis(
        thesis_id=f"TH-{trade_id}",
        trade_id=trade_id,
        snapshot_id=snapshot_id,
        written_at=written_at,
        author=DeskRole.ENTRY,
        model_id=ENTRY_MODEL_ID,
        prompt_version=ENTRY_PROMPT_VERSION,
        directional_claim=DirectionalClaim.BULLISH,
        horizon_days=5,
        primary_driver=DriverCode.TREND_CONTINUATION,
        supporting_reason_codes=("REGIME_ALIGNMENT",),
        contradicting_reason_codes=("EVENT_CLEAR",),
        invalidation=(inv_a, inv_b),
        strengthening=(),
        confidence=Decimal("0.5"),
        confidence_kind=ConfidenceKind.RAW_SCORE,
        expected_mfe_r=None,
        expected_mae_r=None,
        thesis_hash="shadow-stub-thesis-hash",
    )


# Re-export gate id used when callers want an explicit shortlist skip marker.
SHORTLIST_TOO_SMALL = EntryGateId.SHORTLIST_TOO_SMALL
