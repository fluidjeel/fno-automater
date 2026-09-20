"""ADESK-C3: continuous TerminalPolicy enforcement + one-way revert."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

import tests.factories as f
from trading.ai.terminal_enforcement import (
    TerminalEnforcementOutcome,
    attempt_regrant_run_to_expiry,
    enforce_terminal_run_conditions,
    refuse_regrant_run_to_expiry,
    revert_to_flatten_at_dte,
)
from trading.domain.contracts.position import ExitPolicy
from trading.domain.contracts.terminal_policy import DEFAULT_FLATTEN_DTE, TerminalPolicy
from trading.domain.contracts.trade_thesis import InvalidationCondition
from trading.domain.enums import (
    Comparator,
    InvalidationMetric,
    InvalidationSeverity,
    TerminalPolicyKind,
)
from trading.domain.primitives import Currency, Money
from trading.trade.exits import attach_terminal_policy

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
INR = Currency.INR


def _money(v: str) -> Money:
    return Money.of(v, INR)


def _condition(
    cid: str = "RC-1",
    *,
    threshold: str = "-3",
) -> InvalidationCondition:
    return InvalidationCondition(
        condition_id=cid,
        metric=InvalidationMetric.SPOT_PCT_FROM_ENTRY,
        comparator=Comparator.LT,
        threshold=Decimal(threshold),
        severity=InvalidationSeverity.HARD,
    )


def _run_policy(trade_id: str = "TRD-1") -> TerminalPolicy:
    return TerminalPolicy(
        policy_id="TP-1",
        trade_id=trade_id,
        kind=TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK,
        flatten_dte=None,
        run_conditions=(_condition(),),
        max_terminal_loss=_money("5000"),
        accepted_at=NOW,
        requested_kind=TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK,
    )


def _exit_with_run() -> ExitPolicy:
    return attach_terminal_policy(f.exit_policy(), _run_policy())


def test_all_conditions_hold_continues() -> None:
    policy = _exit_with_run()
    # SPOT_PCT_FROM_ENTRY = -1, condition LT -3 → not triggered → hold
    result = enforce_terminal_run_conditions(
        policy,
        observations={InvalidationMetric.SPOT_PCT_FROM_ENTRY: Decimal("-1")},
        days_to_expiry=5,
    )
    assert result.outcome is TerminalEnforcementOutcome.CONTINUE
    assert result.updated_policy is None
    assert result.broken_condition_ids == ()


def test_broken_condition_reverts_to_flatten() -> None:
    policy = _exit_with_run()
    # -5 < -3 → triggered → break
    result = enforce_terminal_run_conditions(
        policy,
        observations={InvalidationMetric.SPOT_PCT_FROM_ENTRY: Decimal("-5")},
        days_to_expiry=5,
    )
    assert result.outcome is TerminalEnforcementOutcome.REVERTED_TO_FLATTEN
    assert result.reverted is True
    assert result.broken_condition_ids == ("RC-1",)
    assert result.updated_policy is not None
    tp = result.updated_policy.terminal_policy
    assert tp is not None
    assert tp.kind is TerminalPolicyKind.FLATTEN_AT_DTE
    assert tp.flatten_dte == DEFAULT_FLATTEN_DTE
    assert tp.run_conditions == ()
    assert result.updated_policy.exit_before_expiry_days == DEFAULT_FLATTEN_DTE
    # requested_kind preserved for one-way door
    assert tp.requested_kind is TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK


def test_broken_inside_dte_requires_exit() -> None:
    policy = _exit_with_run()
    result = enforce_terminal_run_conditions(
        policy,
        observations={InvalidationMetric.SPOT_PCT_FROM_ENTRY: Decimal("-5")},
        days_to_expiry=1,
    )
    assert result.outcome is TerminalEnforcementOutcome.EXIT_INSIDE_DTE
    assert result.requires_exit is True
    assert result.updated_policy is not None
    assert (
        result.updated_policy.terminal_policy.kind  # type: ignore[union-attr]
        is TerminalPolicyKind.FLATTEN_AT_DTE
    )


def test_missing_observation_fail_closed() -> None:
    policy = _exit_with_run()
    result = enforce_terminal_run_conditions(
        policy,
        observations={},
        days_to_expiry=5,
    )
    assert result.outcome is TerminalEnforcementOutcome.REVERTED_TO_FLATTEN
    assert result.broken_condition_ids == ("RC-1",)


def test_one_way_door_refuses_regrant_after_revert() -> None:
    policy = _exit_with_run()
    reverted = revert_to_flatten_at_dte(policy, flatten_dte=1)
    with pytest.raises(ValueError, match="one-way door"):
        refuse_regrant_run_to_expiry(reverted)
    with pytest.raises(ValueError, match="one-way door"):
        attempt_regrant_run_to_expiry(reverted, _run_policy())


def test_attach_still_refuses_replace_after_revert() -> None:
    policy = _exit_with_run()
    reverted = revert_to_flatten_at_dte(policy, flatten_dte=1)
    with pytest.raises(ValueError, match="already carries"):
        attach_terminal_policy(reverted, _run_policy())


def test_no_policy_is_noop() -> None:
    result = enforce_terminal_run_conditions(
        f.exit_policy(),
        observations={InvalidationMetric.SPOT_PCT_FROM_ENTRY: Decimal("-5")},
        days_to_expiry=1,
    )
    assert result.outcome is TerminalEnforcementOutcome.NO_TERMINAL_POLICY
