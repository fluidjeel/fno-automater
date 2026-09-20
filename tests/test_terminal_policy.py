"""ADESK-C1: TerminalPolicy contracts + PART 5.2 eligibility gate.

Matrix: each of seven conditions fails alone → FLATTEN_AT_DTE fallback;
all pass → RUN_TO_EXPIRY_DEFINED_RISK. Zero LLM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading.ai.terminal_policy import (
    DEFAULT_FLATTEN_DTE,
    accept_terminal_policy,
    evaluate_run_to_expiry_eligibility,
    structure_is_prepaid_defined_risk,
)
from trading.domain.contracts.identification import StructureKind
from trading.domain.contracts.terminal_policy import (
    TerminalPolicyEligibilityInput,
    TerminalPolicyRequest,
)
from trading.domain.contracts.trade_thesis import InvalidationCondition
from trading.domain.enums import (
    Comparator,
    InvalidationMetric,
    InvalidationSeverity,
    LiquidityGrade,
    ReasonCode,
    TerminalPolicyKind,
    TerminalPolicyRejectReason,
)
from trading.domain.primitives import Currency, Money
from trading.news.contracts import EventRiskStatus

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
INR = Currency.INR


def _money(v: str) -> Money:
    return Money.of(v, INR)


def _condition(cid: str = "RC-1") -> InvalidationCondition:
    return InvalidationCondition(
        condition_id=cid,
        metric=InvalidationMetric.SPOT_PCT_FROM_ENTRY,
        comparator=Comparator.LT,
        threshold=Decimal("24000"),
        severity=InvalidationSeverity.HARD,
    )


def _request(
    *,
    kind: TerminalPolicyKind = TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK,
    flatten_dte: int | None = None,
) -> TerminalPolicyRequest:
    return TerminalPolicyRequest(
        kind=kind,
        flatten_dte=flatten_dte,
        run_conditions=(
            (_condition(),) if kind is not TerminalPolicyKind.FLATTEN_AT_DTE else ()
        ),
        max_terminal_loss=_money("5000"),
        rationale_codes=(ReasonCode.OK,),
    )


def _eligibility(**overrides: object) -> TerminalPolicyEligibilityInput:
    payload: dict[str, object] = {
        "structure_kind": StructureKind.DEBIT_SPREAD,
        "assignment_risk_at_expiry": False,
        "cash_settled": True,
        "approved_max_loss": _money("5000"),
        "recomputed_max_loss": _money("5000"),
        "max_loss_tolerance": _money("100"),
        "extrinsic_pct": Decimal("0.02"),
        "extrinsic_floor_pct": Decimal("0.05"),
        "position_notional": _money("10000"),
        "existing_terminal_notional": _money("0"),
        "equity": _money("1000000"),
        "terminal_exposure_cap_fraction": Decimal("0.10"),
        "liquidity_grade": LiquidityGrade.A,
        "event_risk_status": EventRiskStatus.NORMAL,
    }
    payload.update(overrides)
    return TerminalPolicyEligibilityInput.model_validate(payload)


class TestStructureHelper:
    def test_long_option_and_debit_qualify(self) -> None:
        assert structure_is_prepaid_defined_risk(
            StructureKind.LONG_OPTION, assignment_risk_at_expiry=False
        )
        assert structure_is_prepaid_defined_risk(
            StructureKind.DEBIT_SPREAD, assignment_risk_at_expiry=False
        )

    def test_credit_future_and_assignment_fail(self) -> None:
        assert not structure_is_prepaid_defined_risk(
            StructureKind.CREDIT_SPREAD, assignment_risk_at_expiry=False
        )
        assert not structure_is_prepaid_defined_risk(
            StructureKind.COMMODITY_FUTURE, assignment_risk_at_expiry=False
        )
        assert not structure_is_prepaid_defined_risk(
            StructureKind.LONG_OPTION, assignment_risk_at_expiry=True
        )


class TestEligibilityMatrix:
    """Each PART 5.2 condition fails alone → that reject reason; all pass → empty."""

    def test_all_pass(self) -> None:
        assert evaluate_run_to_expiry_eligibility(_eligibility()) == ()

    @pytest.mark.parametrize(
        ("override", "reason"),
        [
            (
                {"structure_kind": StructureKind.CREDIT_SPREAD},
                TerminalPolicyRejectReason.STRUCTURE_NOT_DEFINED_RISK,
            ),
            (
                {"assignment_risk_at_expiry": True},
                TerminalPolicyRejectReason.ASSIGNMENT_RISK_AT_EXPIRY,
            ),
            (
                {"cash_settled": False},
                TerminalPolicyRejectReason.NOT_CASH_SETTLED,
            ),
            (
                {"recomputed_max_loss": _money("6000")},
                TerminalPolicyRejectReason.MAX_LOSS_MISMATCH,
            ),
            (
                {"extrinsic_pct": Decimal("0.20")},
                TerminalPolicyRejectReason.EXTRINSIC_ABOVE_FLOOR,
            ),
            (
                {
                    "position_notional": _money("80000"),
                    "existing_terminal_notional": _money("50000"),
                },
                TerminalPolicyRejectReason.TERMINAL_EXPOSURE_CAP,
            ),
            (
                {"liquidity_grade": LiquidityGrade.B},
                TerminalPolicyRejectReason.LIQUIDITY_BELOW_A,
            ),
            (
                {"event_risk_status": EventRiskStatus.CAUTION},
                TerminalPolicyRejectReason.EVENT_RISK_NOT_NORMAL,
            ),
        ],
        ids=[
            "structure",
            "assignment",
            "cash_settled",
            "max_loss",
            "extrinsic",
            "exposure_cap",
            "liquidity",
            "event_risk",
        ],
    )
    def test_each_condition_fails_alone(
        self, override: dict[str, object], reason: TerminalPolicyRejectReason
    ) -> None:
        reasons = evaluate_run_to_expiry_eligibility(_eligibility(**override))
        assert reasons == (reason,)


class TestAcceptGate:
    def test_all_pass_accepts_run_to_expiry(self) -> None:
        decision = accept_terminal_policy(
            _request(),
            trade_id="T-1",
            as_of=NOW,
            eligibility=_eligibility(),
            policy_id="TP-1",
        )
        assert decision.eligible is True
        assert decision.reject_reasons == ()
        assert decision.accepted.kind is TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK
        assert decision.accepted.run_conditions
        assert decision.accepted.flatten_dte is None
        assert decision.accepted.reject_reasons == ()

    @pytest.mark.parametrize(
        "override",
        [
            {"structure_kind": StructureKind.CREDIT_SPREAD},
            {"assignment_risk_at_expiry": True},
            {"cash_settled": False},
            {"recomputed_max_loss": _money("6000")},
            {"extrinsic_pct": Decimal("0.20")},
            {
                "position_notional": _money("80000"),
                "existing_terminal_notional": _money("50000"),
            },
            {"liquidity_grade": LiquidityGrade.B},
            {"event_risk_status": EventRiskStatus.CAUTION},
        ],
    )
    def test_any_miss_falls_back_to_flatten_at_dte(
        self, override: dict[str, object]
    ) -> None:
        decision = accept_terminal_policy(
            _request(),
            trade_id="T-1",
            as_of=NOW,
            eligibility=_eligibility(**override),
            policy_id="TP-FB",
        )
        assert decision.eligible is False
        assert decision.reject_reasons
        assert decision.accepted.kind is TerminalPolicyKind.FLATTEN_AT_DTE
        assert decision.accepted.flatten_dte == DEFAULT_FLATTEN_DTE
        assert decision.accepted.run_conditions == ()
        assert decision.accepted.requested_kind is (
            TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK
        )
        assert decision.accepted.reject_reasons == decision.reject_reasons

    def test_max_loss_mismatch_sets_hallucination_flag(self) -> None:
        decision = accept_terminal_policy(
            _request(),
            trade_id="T-1",
            as_of=NOW,
            eligibility=_eligibility(recomputed_max_loss=_money("9000")),
        )
        assert decision.accepted.max_loss_hallucination is True
        assert TerminalPolicyRejectReason.MAX_LOSS_MISMATCH in decision.reject_reasons

    def test_flatten_at_dte_passthrough(self) -> None:
        req = TerminalPolicyRequest(
            kind=TerminalPolicyKind.FLATTEN_AT_DTE,
            flatten_dte=2,
            run_conditions=(),
            max_terminal_loss=_money("5000"),
            rationale_codes=(),
        )
        decision = accept_terminal_policy(req, trade_id="T-1", as_of=NOW)
        assert decision.eligible is True
        assert decision.accepted.kind is TerminalPolicyKind.FLATTEN_AT_DTE
        assert decision.accepted.flatten_dte == 2

    def test_run_to_expiry_requires_eligibility(self) -> None:
        with pytest.raises(ValueError, match="EligibilityInput"):
            accept_terminal_policy(_request(), trade_id="T-1", as_of=NOW)

    def test_request_requires_run_conditions_for_run_to_expiry(self) -> None:
        with pytest.raises(ValidationError):
            TerminalPolicyRequest(
                kind=TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK,
                flatten_dte=None,
                run_conditions=(),
                max_terminal_loss=_money("5000"),
            )

    def test_frozen_policy_is_immutable(self) -> None:
        decision = accept_terminal_policy(
            _request(),
            trade_id="T-1",
            as_of=NOW,
            eligibility=_eligibility(),
            policy_id="TP-IMM",
        )
        with pytest.raises(ValidationError):
            decision.accepted.kind = TerminalPolicyKind.FLATTEN_AT_DTE
