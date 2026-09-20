"""ADESK-C4: terminal-policy cost vs counterfactual flatten."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading.analytics.terminal_policy_cost import (
    TerminalPolicyOutcomeRow,
    build_terminal_policy_cost_report,
    cost_saved_inr,
    cost_saved_r,
)
from trading.domain.enums import TerminalPolicyKind
from trading.domain.primitives import Currency, Money

AS_OF = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def _row(**overrides: object) -> TerminalPolicyOutcomeRow:
    payload: dict[str, object] = {
        "trade_id": "t1",
        "policy_kind": TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK,
        "held_through_flatten_dte": True,
        "round_trip_charges_inr": Money.of(Decimal("50"), Currency.INR),
        "round_trip_spread_inr": Money.of(Decimal("30"), Currency.INR),
        "r_unit_inr": Money.of(Decimal("1000"), Currency.INR),
    }
    payload.update(overrides)
    return TerminalPolicyOutcomeRow.model_validate(payload)


def test_run_to_expiry_held_saves_round_trip() -> None:
    row = _row()
    assert cost_saved_inr(row) == Money.of(Decimal("80"), Currency.INR)
    assert cost_saved_r(row) == Decimal("0.0800")


def test_flatten_policy_saves_zero() -> None:
    row = _row(policy_kind=TerminalPolicyKind.FLATTEN_AT_DTE)
    assert cost_saved_inr(row).amount == 0
    assert cost_saved_r(row) == Decimal("0.0000")


def test_run_but_not_held_saves_zero() -> None:
    row = _row(held_through_flatten_dte=False)
    assert cost_saved_inr(row).amount == 0


def test_report_aggregates_r_and_inr() -> None:
    rows = (
        _row(trade_id="a"),
        _row(
            trade_id="b",
            policy_kind=TerminalPolicyKind.FLATTEN_AT_DTE,
            held_through_flatten_dte=False,
        ),
        _row(
            trade_id="c",
            round_trip_charges_inr=Money.of(Decimal("100"), Currency.INR),
            round_trip_spread_inr=Money.of(Decimal("20"), Currency.INR),
            r_unit_inr=Money.of(Decimal("500"), Currency.INR),
        ),
    )
    report = build_terminal_policy_cost_report(rows, as_of=AS_OF)
    assert report.trade_count == 3
    assert report.run_to_expiry_held_count == 2
    # a: 80, c: 120 → 200 INR; R: 0.08 + 0.24 = 0.32
    assert report.total_cost_saved_inr == Money.of(Decimal("200"), Currency.INR)
    assert report.total_cost_saved_r == Decimal("0.3200")


def test_empty_report_is_zero_inr() -> None:
    report = build_terminal_policy_cost_report((), as_of=AS_OF)
    assert report.total_cost_saved_inr.amount == 0
    assert report.total_cost_saved_r == Decimal("0")
    assert report.trade_count == 0


def test_rejects_non_positive_r() -> None:
    with pytest.raises(ValueError, match="r_unit_inr"):
        _row(r_unit_inr=Money.of(Decimal("0"), Currency.INR))
