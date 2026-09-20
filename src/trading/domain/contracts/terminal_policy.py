"""TerminalPolicy contracts and eligibility DTOs (ADESK-C1 / PART 5).

Run-to-expiry is decided at entry, never at T-1. The accepted policy is frozen
and later enforced deterministically. No free-text fields except none — all
reasons are closed enums.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.identification import StructureKind
from trading.domain.contracts.trade_thesis import InvalidationCondition
from trading.domain.enums import (
    LiquidityGrade,
    ReasonCode,
    TerminalPolicyKind,
    TerminalPolicyRejectReason,
)
from trading.domain.primitives import Money
from trading.news.contracts import EventRiskStatus

__all__ = [
    "DEFAULT_FLATTEN_DTE",
    "TerminalPolicy",
    "TerminalPolicyDecision",
    "TerminalPolicyEligibilityInput",
    "TerminalPolicyRequest",
]

DEFAULT_FLATTEN_DTE = 1


class TerminalPolicyRequest(StrictModel):
    """ENTRY desk (or deterministic default) request for a terminal policy."""

    kind: TerminalPolicyKind
    flatten_dte: StrictInt | None = Field(default=None, ge=0)
    run_conditions: tuple[InvalidationCondition, ...] = ()
    max_terminal_loss: Money
    rationale_codes: tuple[ReasonCode, ...] = ()

    @model_validator(mode="after")
    def _kind_fields(self) -> Self:
        if self.kind is TerminalPolicyKind.FLATTEN_AT_DTE and self.flatten_dte is None:
            raise ValueError("FLATTEN_AT_DTE requires flatten_dte")
        if (
            self.kind is TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK
            and not self.run_conditions
        ):
            raise ValueError(
                "RUN_TO_EXPIRY_DEFINED_RISK requires at least one run_condition"
            )
        return self


class TerminalPolicyEligibilityInput(StrictModel):
    """Facts for the PART 5.2 seven-condition gate. Callers resolve instruments."""

    structure_kind: StructureKind
    assignment_risk_at_expiry: StrictBool
    cash_settled: StrictBool
    approved_max_loss: Money
    recomputed_max_loss: Money
    max_loss_tolerance: Money
    extrinsic_pct: ExactDecimal = Field(ge=Decimal(0))
    extrinsic_floor_pct: ExactDecimal = Field(ge=Decimal(0))
    position_notional: Money
    existing_terminal_notional: Money
    equity: Money
    terminal_exposure_cap_fraction: ExactDecimal = Field(gt=Decimal(0), le=Decimal(1))
    liquidity_grade: LiquidityGrade
    event_risk_status: EventRiskStatus

    @model_validator(mode="after")
    def _same_currency(self) -> Self:
        currencies = {
            self.approved_max_loss.currency,
            self.recomputed_max_loss.currency,
            self.max_loss_tolerance.currency,
            self.position_notional.currency,
            self.existing_terminal_notional.currency,
            self.equity.currency,
        }
        if len(currencies) != 1:
            raise ValueError(
                "TerminalPolicyEligibilityInput mixes currencies "
                f"{sorted(c.value for c in currencies)}"
            )
        return self


class TerminalPolicy(VersionedModel):
    """Accepted, frozen terminal policy snapshot bound to a trade at entry."""

    policy_id: NonEmptyStr
    trade_id: NonEmptyStr
    kind: TerminalPolicyKind
    flatten_dte: StrictInt | None = Field(default=None, ge=0)
    run_conditions: tuple[InvalidationCondition, ...] = ()
    max_terminal_loss: Money
    accepted_at: UtcDatetime
    requested_kind: TerminalPolicyKind
    reject_reasons: tuple[TerminalPolicyRejectReason, ...] = ()
    max_loss_hallucination: StrictBool = False

    @model_validator(mode="after")
    def _accepted_shape(self) -> Self:
        if self.kind is TerminalPolicyKind.FLATTEN_AT_DTE and self.flatten_dte is None:
            raise ValueError("accepted FLATTEN_AT_DTE requires flatten_dte")
        if (
            self.kind is TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK
            and self.reject_reasons
        ):
            raise ValueError("RUN_TO_EXPIRY_DEFINED_RISK cannot carry reject_reasons")
        return self


class TerminalPolicyDecision(StrictModel):
    """Gate outcome: always an accepted TerminalPolicy (possibly fallback)."""

    accepted: TerminalPolicy
    eligible: StrictBool
    reject_reasons: tuple[TerminalPolicyRejectReason, ...] = ()
