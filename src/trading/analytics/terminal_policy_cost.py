"""ADESK-C4: terminal-policy cost vs counterfactual flatten (PART 5.4).

Running a fully ITM defined-risk debit to expiry saves roughly one round-trip
(charges + bid-ask). This is a *cost* line, not alpha. Pure arithmetic; no LLM.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import TerminalPolicyKind
from trading.domain.primitives import Currency, Money

__all__ = [
    "TerminalPolicyCostLine",
    "TerminalPolicyCostReport",
    "TerminalPolicyOutcomeRow",
    "build_terminal_policy_cost_report",
    "cost_saved_inr",
    "cost_saved_r",
]


class TerminalPolicyOutcomeRow(StrictModel):
    """One closed trade with enough facts to score terminal-policy cost."""

    trade_id: NonEmptyStr
    policy_kind: TerminalPolicyKind
    held_through_flatten_dte: bool
    """True when the position was still open past the flatten DTE (ran further)."""
    round_trip_charges_inr: Money
    """Charges that would have been paid on a flatten exit (one RT)."""
    round_trip_spread_inr: Money
    """Estimated bid-ask paid on a flatten exit (all legs)."""
    r_unit_inr: Money
    """1R in INR for this trade (approved risk). Must be > 0."""

    @model_validator(mode="after")
    def _currency_and_r(self) -> TerminalPolicyOutcomeRow:
        currencies = {
            self.round_trip_charges_inr.currency,
            self.round_trip_spread_inr.currency,
            self.r_unit_inr.currency,
        }
        if len(currencies) != 1:
            raise ValueError("TerminalPolicyOutcomeRow mixes currencies")
        if self.r_unit_inr.amount <= 0:
            raise ValueError("r_unit_inr must be > 0")
        if (
            self.round_trip_charges_inr.amount < 0
            or self.round_trip_spread_inr.amount < 0
        ):
            raise ValueError("round-trip costs must be >= 0")
        return self


def cost_saved_inr(row: TerminalPolicyOutcomeRow) -> Money:
    """INR saved vs counterfactual flatten-at-DTE exit.

    Only RUN_TO_EXPIRY_DEFINED_RISK trades that actually held past flatten DTE
    save the flatten round-trip. Everything else scores zero.
    """
    currency = row.round_trip_charges_inr.currency
    if (
        row.policy_kind is not TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK
        or not row.held_through_flatten_dte
    ):
        return Money.zero(currency)
    total = row.round_trip_charges_inr.amount + row.round_trip_spread_inr.amount
    return Money.of(total, currency)


def cost_saved_r(row: TerminalPolicyOutcomeRow) -> Decimal:
    saved = cost_saved_inr(row)
    return (saved.amount / row.r_unit_inr.amount).quantize(Decimal("0.0001"))


class TerminalPolicyCostLine(StrictModel):
    trade_id: NonEmptyStr
    policy_kind: TerminalPolicyKind
    held_through_flatten_dte: bool
    cost_saved_inr: Money
    cost_saved_r: ExactDecimal


class TerminalPolicyCostReport(VersionedModel):
    """Aggregate cost-saved report for ADESK-C4 / PART 5.4."""

    as_of: UtcDatetime
    lines: tuple[TerminalPolicyCostLine, ...] = ()
    total_cost_saved_inr: Money
    total_cost_saved_r: ExactDecimal
    trade_count: int = Field(ge=0)
    run_to_expiry_held_count: int = Field(ge=0)


def build_terminal_policy_cost_report(
    rows: Sequence[TerminalPolicyOutcomeRow],
    *,
    as_of: datetime,
) -> TerminalPolicyCostReport:
    if not rows:
        zero = Money.zero(Currency.INR)
        return TerminalPolicyCostReport(
            as_of=as_of,
            lines=(),
            total_cost_saved_inr=zero,
            total_cost_saved_r=Decimal("0"),
            trade_count=0,
            run_to_expiry_held_count=0,
        )

    lines: list[TerminalPolicyCostLine] = []
    total_inr = Decimal("0")
    total_r = Decimal("0")
    held = 0
    currency = rows[0].round_trip_charges_inr.currency
    for row in rows:
        saved = cost_saved_inr(row)
        saved_r = cost_saved_r(row)
        if (
            row.policy_kind is TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK
            and row.held_through_flatten_dte
        ):
            held += 1
        total_inr += saved.amount
        total_r += saved_r
        lines.append(
            TerminalPolicyCostLine(
                trade_id=row.trade_id,
                policy_kind=row.policy_kind,
                held_through_flatten_dte=row.held_through_flatten_dte,
                cost_saved_inr=saved,
                cost_saved_r=saved_r,
            )
        )
    return TerminalPolicyCostReport(
        as_of=as_of,
        lines=tuple(lines),
        total_cost_saved_inr=Money.of(total_inr, currency),
        total_cost_saved_r=total_r.quantize(Decimal("0.0001")),
        trade_count=len(rows),
        run_to_expiry_held_count=held,
    )
