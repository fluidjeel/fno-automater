"""ENTRY desk SHADOW/ADVISORY + config-promotion BOUNDED (ADESK-B2 / D5).

Deterministic shadow advice + DecisionLog only. No LLM on the intraday path.
Does not mutate OMS, broker, gateway decisions, or paper fill outcomes.

C1 / D5: VETO_ENTRY / REDUCE_SIZE are SHADOW/ADVISORY only. BOUNDED is
allowed solely for config-promotion PROPOSE_* actions.
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
from trading.domain.contracts.confidence_sizing import Phase1SizingAdvice
from trading.domain.contracts.entry import (
    EntryAdvice,
    StrikeShortlist,
    assert_candidate_on_shortlist,
)
from trading.domain.contracts.identification import ConfidenceKind
from trading.domain.contracts.trade_thesis import InvalidationCondition, TradeThesis
from trading.domain.enums import (
    BOUNDED_ACTIONS,
    AgentAction,
    AuthorityMode,
    Comparator,
    ConfidenceBucket,
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
    "ENTRY_CONFIG_PACKET_VERSION",
    "ENTRY_CONFIG_POLICY_VERSION",
    "ENTRY_CONFIG_PROMPT_VERSION",
    "ENTRY_LIVE_PATH_ACTIONS",
    "ENTRY_MODEL_ID",
    "ENTRY_PACKET_VERSION",
    "ENTRY_POLICY_VERSION",
    "ENTRY_PROMPT_VERSION",
    "EntryConfigPromotionResult",
    "EntryShadowResult",
    "build_shadow_entry_advice",
    "entry_mode_for_grant",
    "maybe_log_entry_config_promotion",
    "maybe_log_entry_shadow",
    "refuse_bounded_entry_live_path",
    "shadow_decision_from_advice",
    "sizing_advice_for_bucket",
]

ENTRY_PROMPT_VERSION = "entry-shadow-v1"
ENTRY_PACKET_VERSION = "entry-packet-v1"
ENTRY_POLICY_VERSION = "entry-policy-v1"
ENTRY_MODEL_ID = "deterministic-shadow"
ENTRY_CONFIG_PROMPT_VERSION = "entry-config-v1"
ENTRY_CONFIG_POLICY_VERSION = "entry-config-policy-v1"
ENTRY_CONFIG_PACKET_VERSION = "entry-config-packet-v1"

ENTRY_LIVE_PATH_ACTIONS: frozenset[AgentAction] = frozenset(
    {
        AgentAction.VETO_ENTRY,
        AgentAction.REDUCE_SIZE,
    }
)


@dataclass(frozen=True, slots=True)
class EntryShadowResult:
    """Outcome of a SHADOW/ADVISORY ENTRY hook. Never an order instruction."""

    status: Literal[
        "LOGGED",
        "SKIPPED_DISABLED",
        "SKIPPED_SHORTLIST",
        "PASS",
        "REJECTED_BOUNDED",
    ]
    advice: EntryAdvice | None
    decision: AgentDecision | None
    mode: AuthorityMode = AuthorityMode.SHADOW


def sizing_advice_for_bucket(
    bucket: ConfidenceBucket,
) -> Phase1SizingAdvice:
    """Shared Phase-1 sizing advice for ENTRY (and other desks). Never live BOUNDED."""
    return Phase1SizingAdvice.from_bucket(bucket)


def build_shadow_entry_advice(
    shortlist: StrikeShortlist,
    *,
    as_of: datetime,
    trade_id: str = "SHADOW-TRADE",
    confidence_bucket: ConfidenceBucket = ConfidenceBucket.P90,
) -> EntryAdvice | None:
    """Build deterministic SELECT_STRIKE_CANDIDATE for the top-scored candidate.

    Returns None when shortlist has fewer than two candidates (skip agent / PASS).
    size_multiplier comes from the Phase-1 ConfidenceBucket map (always <= 1.0).
    """
    if not shortlist.eligible_for_agent:
        return None
    top = shortlist.top_by_score()
    if top is None:
        return None
    sizing = sizing_advice_for_bucket(confidence_bucket)
    thesis = _stub_thesis(
        trade_id=trade_id,
        snapshot_id=shortlist.snapshot_id,
        written_at=as_of,
        confidence=Decimal(confidence_bucket.value),
    )
    advice = EntryAdvice(
        as_of=as_of,
        snapshot_id=shortlist.snapshot_id,
        action=AgentAction.SELECT_STRIKE_CANDIDATE,
        candidate_id=top.candidate_id,
        size_multiplier=sizing.size_multiplier,
        thesis=thesis,
        evidence_ids=("shortlist-deterministic",),
        failed_gate_ids=(),
        narrative=(
            "SHADOW deterministic top-of-shortlist; Phase-1 downscale sizing; "
            "no live BOUNDED influence."
        ),
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
    mode: AuthorityMode = AuthorityMode.SHADOW,
) -> AgentDecision:
    """Map EntryAdvice onto AgentDecision with gate_outcome SHADOW_ONLY."""
    return AgentDecision(
        decision_id=decision_id or f"DEC-ENTRY-{uuid4().hex[:12]}",
        run_id=run_id,
        role=DeskRole.ENTRY,
        mode=mode,
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


def refuse_bounded_entry_live_path(mode: AuthorityMode, action: AgentAction) -> bool:
    """True when VETO_ENTRY/REDUCE_SIZE is illegally paired with BOUNDED."""
    return mode is AuthorityMode.BOUNDED and action in ENTRY_LIVE_PATH_ACTIONS


def entry_mode_for_grant(
    grant: AuthorityGrant | None,
    *,
    now: datetime,
    prompt_version: str = ENTRY_PROMPT_VERSION,
    policy_version: str = ENTRY_POLICY_VERSION,
) -> AuthorityMode:
    return resolve_effective_mode(
        DeskRole.ENTRY,
        runtime_model_id=ENTRY_MODEL_ID,
        runtime_prompt_version=prompt_version,
        runtime_policy_version=policy_version,
        now=now,
        grant=grant,
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
    grant: AuthorityGrant | None = None,
) -> EntryShadowResult:
    """Paper-entry hook: log SHADOW/ADVISORY EntryAdvice; never affect fills.

    When enabled is False the live/paper path is unchanged (no log, no advice).
    When shortlist has < 2 candidates, skip the agent; no decision is written.
    BOUNDED is refused for VETO_ENTRY / REDUCE_SIZE.
    """
    if not enabled:
        return EntryShadowResult(
            status="SKIPPED_DISABLED",
            advice=None,
            decision=None,
            mode=AuthorityMode.OBSERVE,
        )
    if not shortlist.eligible_for_agent:
        return EntryShadowResult(
            status="SKIPPED_SHORTLIST",
            advice=None,
            decision=None,
            mode=AuthorityMode.OBSERVE,
        )
    mode = entry_mode_for_grant(grant, now=as_of)
    if grant is None:
        mode = AuthorityMode.SHADOW
    advice = build_shadow_entry_advice(
        shortlist, as_of=as_of, trade_id=trade_id or "SHADOW-TRADE"
    )
    if advice is None:
        return EntryShadowResult(
            status="PASS", advice=None, decision=None, mode=mode
        )
    if refuse_bounded_entry_live_path(mode, advice.action):
        return EntryShadowResult(
            status="REJECTED_BOUNDED",
            advice=advice,
            decision=None,
            mode=mode,
        )
    decision = shadow_decision_from_advice(
        advice,
        run_id=run_id,
        trade_id=trade_id,
        environment=environment,
        mode=mode,
    )
    if decision_log is not None:
        decision_log.record(decision)
    return EntryShadowResult(
        status="LOGGED", advice=advice, decision=decision, mode=mode
    )


@dataclass(frozen=True, slots=True)
class EntryConfigPromotionResult:
    status: Literal["LOGGED", "SKIPPED_DISABLED", "OBSERVE", "REJECTED_NOT_BOUNDED"]
    decision: AgentDecision | None
    mode: AuthorityMode


def maybe_log_entry_config_promotion(
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
) -> EntryConfigPromotionResult:
    """BOUNDED path for PROPOSE_* only. Live-path veto/reduce cannot use this."""
    if not enabled:
        return EntryConfigPromotionResult(
            status="SKIPPED_DISABLED",
            decision=None,
            mode=AuthorityMode.OBSERVE,
        )
    if action not in BOUNDED_ACTIONS:
        return EntryConfigPromotionResult(
            status="REJECTED_NOT_BOUNDED",
            decision=None,
            mode=AuthorityMode.OBSERVE,
        )
    mode = entry_mode_for_grant(
        grant,
        now=as_of,
        prompt_version=ENTRY_CONFIG_PROMPT_VERSION,
        policy_version=ENTRY_CONFIG_POLICY_VERSION,
    )
    if mode is not AuthorityMode.BOUNDED or grant is None:
        return EntryConfigPromotionResult(
            status="OBSERVE", decision=None, mode=mode
        )
    if (
        action not in grant.allowed_actions
        or strategy_family not in grant.strategy_families
    ):
        return EntryConfigPromotionResult(
            status="REJECTED_NOT_BOUNDED",
            decision=None,
            mode=mode,
        )
    decision = AgentDecision(
        decision_id=f"DEC-EN-CFG-{uuid4().hex[:12]}",
        run_id=run_id,
        role=DeskRole.ENTRY,
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
        model_id=ENTRY_MODEL_ID,
        prompt_version=ENTRY_CONFIG_PROMPT_VERSION,
        policy_version=ENTRY_CONFIG_POLICY_VERSION,
        packet_version=ENTRY_CONFIG_PACKET_VERSION,
        input_tokens=0,
        output_tokens=0,
        latency_ms=0,
        created_at=as_of,
    )
    if decision_log is not None:
        decision_log.record(decision)
    return EntryConfigPromotionResult(
        status="LOGGED", decision=decision, mode=AuthorityMode.BOUNDED
    )


def _stub_thesis(
    *,
    trade_id: str,
    snapshot_id: str,
    written_at: datetime,
    confidence: Decimal = Decimal("0.5"),
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
        confidence=confidence,
        confidence_kind=ConfidenceKind.RAW_SCORE,
        expected_mfe_r=None,
        expected_mae_r=None,
        thesis_hash="shadow-stub-thesis-hash",
    )


# Re-export gate id used when callers want an explicit shortlist skip marker.
SHORTLIST_TOO_SMALL = EntryGateId.SHORTLIST_TOO_SMALL
