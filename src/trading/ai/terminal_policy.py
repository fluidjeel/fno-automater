"""Deterministic TerminalPolicy eligibility gate (ADESK-C1 / PART 5.2).

Zero LLM. Any failed condition → accepted FLATTEN_AT_DTE with configured
default DTE. Reuses gateway `_is_defined_risk` read-only for spread structures;
long options qualify as fully prepaid defined risk.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from trading.domain.contracts.identification import StructureKind
from trading.domain.contracts.terminal_policy import (
    DEFAULT_FLATTEN_DTE,
    TerminalPolicy,
    TerminalPolicyDecision,
    TerminalPolicyEligibilityInput,
    TerminalPolicyRequest,
)
from trading.domain.enums import (
    LiquidityGrade,
    TerminalPolicyKind,
    TerminalPolicyRejectReason,
)
from trading.news.contracts import EventRiskStatus
from trading.risk.gateway import _is_defined_risk, _StructureKind

__all__ = [
    "DEFAULT_FLATTEN_DTE",
    "accept_terminal_policy",
    "evaluate_run_to_expiry_eligibility",
    "structure_is_prepaid_defined_risk",
]

_STRUCTURE_TO_GATEWAY: dict[StructureKind, _StructureKind] = {
    StructureKind.LONG_OPTION: _StructureKind.LONG_OPTION,
    StructureKind.DEBIT_SPREAD: _StructureKind.DEBIT_SPREAD,
    StructureKind.CREDIT_SPREAD: _StructureKind.CREDIT_SPREAD,
    StructureKind.COMMODITY_FUTURE: _StructureKind.COMMODITY_FUTURE,
}


def structure_is_prepaid_defined_risk(
    structure: StructureKind,
    *,
    assignment_risk_at_expiry: bool,
) -> bool:
    """PART 5.2 condition 1: prepaid max loss, no assignment/futures blowup.

    Long options and debit spreads qualify. Credit spreads may be
    gateway-defined-risk but are refused for terminal run-to-expiry; futures
    never qualify. Assignment risk at expiry is an unconditional veto.
    """
    if assignment_risk_at_expiry:
        return False
    if structure is StructureKind.LONG_OPTION:
        return True
    if structure is StructureKind.DEBIT_SPREAD:
        return _is_defined_risk(_STRUCTURE_TO_GATEWAY[structure])
    return False


def evaluate_run_to_expiry_eligibility(
    eligibility: TerminalPolicyEligibilityInput,
) -> tuple[TerminalPolicyRejectReason, ...]:
    """Return every PART 5.2 condition that fails (empty → eligible)."""
    reasons: list[TerminalPolicyRejectReason] = []

    if eligibility.assignment_risk_at_expiry:
        reasons.append(TerminalPolicyRejectReason.ASSIGNMENT_RISK_AT_EXPIRY)
    elif not structure_is_prepaid_defined_risk(
        eligibility.structure_kind,
        assignment_risk_at_expiry=False,
    ):
        reasons.append(TerminalPolicyRejectReason.STRUCTURE_NOT_DEFINED_RISK)

    if not eligibility.cash_settled:
        reasons.append(TerminalPolicyRejectReason.NOT_CASH_SETTLED)

    loss_delta = (
        eligibility.recomputed_max_loss.amount - eligibility.approved_max_loss.amount
    )
    if loss_delta > eligibility.max_loss_tolerance.amount:
        reasons.append(TerminalPolicyRejectReason.MAX_LOSS_MISMATCH)

    if eligibility.extrinsic_pct > eligibility.extrinsic_floor_pct:
        reasons.append(TerminalPolicyRejectReason.EXTRINSIC_ABOVE_FLOOR)

    combined = (
        eligibility.existing_terminal_notional.amount
        + eligibility.position_notional.amount
    )
    cap = eligibility.equity.amount * eligibility.terminal_exposure_cap_fraction
    if combined > cap:
        reasons.append(TerminalPolicyRejectReason.TERMINAL_EXPOSURE_CAP)

    if eligibility.liquidity_grade is not LiquidityGrade.A:
        reasons.append(TerminalPolicyRejectReason.LIQUIDITY_BELOW_A)

    if eligibility.event_risk_status is not EventRiskStatus.NORMAL:
        reasons.append(TerminalPolicyRejectReason.EVENT_RISK_NOT_NORMAL)

    return tuple(reasons)


def accept_terminal_policy(
    request: TerminalPolicyRequest,
    *,
    trade_id: str,
    as_of: datetime,
    eligibility: TerminalPolicyEligibilityInput | None = None,
    default_flatten_dte: int = DEFAULT_FLATTEN_DTE,
    policy_id: str | None = None,
) -> TerminalPolicyDecision:
    """Validate request; on any PART 5.2 miss accept FLATTEN_AT_DTE fallback.

    FLATTEN_AT_DTE and FLATTEN_EARLY_IF_FRAGILE skip the run-to-expiry gate.
    RUN_TO_EXPIRY_DEFINED_RISK requires eligibility.
    """
    pid = policy_id or f"TP-{uuid4().hex[:12]}"

    if request.kind is TerminalPolicyKind.FLATTEN_AT_DTE:
        accepted = TerminalPolicy(
            policy_id=pid,
            trade_id=trade_id,
            kind=TerminalPolicyKind.FLATTEN_AT_DTE,
            flatten_dte=request.flatten_dte,
            run_conditions=(),
            max_terminal_loss=request.max_terminal_loss,
            accepted_at=as_of,
            requested_kind=request.kind,
            reject_reasons=(),
            max_loss_hallucination=False,
        )
        return TerminalPolicyDecision(
            accepted=accepted, eligible=True, reject_reasons=()
        )

    if request.kind is TerminalPolicyKind.FLATTEN_EARLY_IF_FRAGILE:
        flatten = (
            request.flatten_dte
            if request.flatten_dte is not None
            else default_flatten_dte
        )
        accepted = TerminalPolicy(
            policy_id=pid,
            trade_id=trade_id,
            kind=TerminalPolicyKind.FLATTEN_EARLY_IF_FRAGILE,
            flatten_dte=flatten,
            run_conditions=request.run_conditions,
            max_terminal_loss=request.max_terminal_loss,
            accepted_at=as_of,
            requested_kind=request.kind,
            reject_reasons=(),
            max_loss_hallucination=False,
        )
        return TerminalPolicyDecision(
            accepted=accepted, eligible=True, reject_reasons=()
        )

    if eligibility is None:
        raise ValueError(
            "RUN_TO_EXPIRY_DEFINED_RISK requires TerminalPolicyEligibilityInput"
        )
    reasons = evaluate_run_to_expiry_eligibility(eligibility)
    hallucination = TerminalPolicyRejectReason.MAX_LOSS_MISMATCH in reasons
    if not reasons:
        accepted = TerminalPolicy(
            policy_id=pid,
            trade_id=trade_id,
            kind=TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK,
            flatten_dte=None,
            run_conditions=request.run_conditions,
            max_terminal_loss=request.max_terminal_loss,
            accepted_at=as_of,
            requested_kind=request.kind,
            reject_reasons=(),
            max_loss_hallucination=False,
        )
        return TerminalPolicyDecision(
            accepted=accepted, eligible=True, reject_reasons=()
        )

    fallback_dte = (
        request.flatten_dte if request.flatten_dte is not None else default_flatten_dte
    )
    accepted = TerminalPolicy(
        policy_id=pid,
        trade_id=trade_id,
        kind=TerminalPolicyKind.FLATTEN_AT_DTE,
        flatten_dte=fallback_dte,
        run_conditions=(),
        max_terminal_loss=request.max_terminal_loss,
        accepted_at=as_of,
        requested_kind=request.kind,
        reject_reasons=reasons,
        max_loss_hallucination=hallucination,
    )
    return TerminalPolicyDecision(
        accepted=accepted, eligible=False, reject_reasons=reasons
    )
