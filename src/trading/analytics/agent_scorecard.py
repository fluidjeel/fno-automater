"""Per-role Agent Desk scorecard (ADESK-B10 / PART 14).

Stub zeros are OK when data is missing; every PART 14 metric function exists
and the CLI runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from pydantic import Field

from trading.domain.contracts.agent_decision import AgentDecision
from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictInt,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import DeskRole

__all__ = [
    "AgentDeskScorecard",
    "abstention_rate",
    "build_agent_scorecard",
    "delta_gap_rate",
    "hallucination_rate",
    "inr_per_decision",
    "l0_resolution_rate",
    "median_latency_ms",
    "override_rate",
    "schema_failure_rate",
    "tokens_per_decision",
]


def _ratio(num: int, den: int) -> Decimal:
    if den <= 0:
        return Decimal("0")
    return (Decimal(num) / Decimal(den)).quantize(Decimal("0.0001"))


def l0_resolution_rate(decisions: Sequence[AgentDecision]) -> Decimal:
    """Fraction resolved without escalation tools (stub: non-ABSTAIN share)."""
    if not decisions:
        return Decimal("0")
    resolved = sum(1 for d in decisions if d.action.value != "ABSTAIN")
    return _ratio(resolved, len(decisions))


def schema_failure_rate(decisions: Sequence[AgentDecision]) -> Decimal:
    """Ungrounded/schema failures over decisions (uses ungrounded_codes)."""
    if not decisions:
        return Decimal("0")
    failed = sum(1 for d in decisions if d.ungrounded_codes)
    return _ratio(failed, len(decisions))


def hallucination_rate(decisions: Sequence[AgentDecision]) -> Decimal:
    return schema_failure_rate(decisions)


def delta_gap_rate(gap_count: int = 0, fetch_count: int = 0) -> Decimal:
    if fetch_count <= 0:
        return Decimal("0")
    return _ratio(gap_count, fetch_count)


def median_latency_ms(decisions: Sequence[AgentDecision]) -> int:
    if not decisions:
        return 0
    values = sorted(d.latency_ms for d in decisions)
    mid = len(values) // 2
    if len(values) % 2:
        return values[mid]
    return (values[mid - 1] + values[mid]) // 2


def tokens_per_decision(decisions: Sequence[AgentDecision]) -> Decimal:
    if not decisions:
        return Decimal("0")
    total = sum(d.input_tokens + d.output_tokens for d in decisions)
    return (Decimal(total) / Decimal(len(decisions))).quantize(Decimal("0.01"))


def inr_per_decision(
    decisions: Sequence[AgentDecision],
    *,
    input_inr_per_m: Decimal = Decimal("15"),
    output_inr_per_m: Decimal = Decimal("60"),
) -> Decimal:
    if not decisions:
        return Decimal("0")
    cost = Decimal("0")
    for d in decisions:
        cost += Decimal(d.input_tokens) * input_inr_per_m / Decimal(1_000_000)
        cost += Decimal(d.output_tokens) * output_inr_per_m / Decimal(1_000_000)
    return (cost / Decimal(len(decisions))).quantize(Decimal("0.0001"))


def abstention_rate(decisions: Sequence[AgentDecision]) -> Decimal:
    if not decisions:
        return Decimal("0")
    abstain = sum(1 for d in decisions if d.action.value == "ABSTAIN")
    return _ratio(abstain, len(decisions))


def override_rate(decisions: Sequence[AgentDecision]) -> Decimal:
    if not decisions:
        return Decimal("0")
    overrides = sum(1 for d in decisions if d.agent_override)
    return _ratio(overrides, len(decisions))


class AgentDeskScorecard(VersionedModel):
    """PART 14 universal metrics plus role-specific stub slots."""

    role: DeskRole
    as_of: UtcDatetime
    decisions: StrictInt = Field(ge=0)
    l0_resolution_rate: ExactDecimal
    schema_failure_rate: ExactDecimal
    hallucination_rate: ExactDecimal
    delta_gap_rate: ExactDecimal
    median_latency_ms: StrictInt = Field(ge=0)
    tokens_per_decision: ExactDecimal
    inr_per_decision: ExactDecimal
    abstention_rate: ExactDecimal
    override_rate: ExactDecimal
    # Role-specific (zeros OK when data missing)
    entry_precision: ExactDecimal | None = None
    entry_capture: ExactDecimal | None = None
    strike_override_uplift_r: ExactDecimal | None = None
    veto_quality_r: ExactDecimal | None = None
    thesis_quality: ExactDecimal | None = None
    review_precision: ExactDecimal | None = None
    premature_exit_cost_r: ExactDecimal | None = None
    saved_loss_r: ExactDecimal | None = None
    warm_cold_divergence: ExactDecimal | None = None
    shared_fate_recall: ExactDecimal | None = None
    macro_classification_accuracy: ExactDecimal | None = None
    stress_coverage: ExactDecimal | None = None
    attribution_stability: ExactDecimal | None = None
    detail: NonEmptyStr = "stub-ready"


def build_agent_scorecard(
    decisions: Sequence[AgentDecision],
    *,
    role: DeskRole,
    as_of: datetime,
    gap_count: int = 0,
    fetch_count: int = 0,
) -> AgentDeskScorecard:
    """Compute universal PART 14 metrics; role-specific fields default None/0."""
    scoped = tuple(d for d in decisions if d.role is role)
    card = AgentDeskScorecard(
        role=role,
        as_of=as_of,
        decisions=len(scoped),
        l0_resolution_rate=l0_resolution_rate(scoped),
        schema_failure_rate=schema_failure_rate(scoped),
        hallucination_rate=hallucination_rate(scoped),
        delta_gap_rate=delta_gap_rate(gap_count, fetch_count),
        median_latency_ms=median_latency_ms(scoped),
        tokens_per_decision=tokens_per_decision(scoped),
        inr_per_decision=inr_per_decision(scoped),
        abstention_rate=abstention_rate(scoped),
        override_rate=override_rate(scoped),
        entry_precision=Decimal("0") if role is DeskRole.ENTRY else None,
        entry_capture=Decimal("0") if role is DeskRole.ENTRY else None,
        strike_override_uplift_r=Decimal("0") if role is DeskRole.ENTRY else None,
        veto_quality_r=Decimal("0") if role is DeskRole.ENTRY else None,
        thesis_quality=Decimal("0") if role is DeskRole.ENTRY else None,
        review_precision=Decimal("0") if role is DeskRole.POSITION else None,
        premature_exit_cost_r=Decimal("0") if role is DeskRole.POSITION else None,
        saved_loss_r=Decimal("0") if role is DeskRole.POSITION else None,
        warm_cold_divergence=Decimal("0") if role is DeskRole.POSITION else None,
        shared_fate_recall=Decimal("0") if role is DeskRole.PORTFOLIO else None,
        macro_classification_accuracy=Decimal("0") if role is DeskRole.MACRO else None,
        stress_coverage=Decimal("0") if role is DeskRole.FRAGILITY else None,
        attribution_stability=Decimal("0") if role is DeskRole.POSTTRADE else None,
    )
    return card
