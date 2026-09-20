"""Deterministic continuous TerminalPolicy enforcement (ADESK-C3 / PART 5.3).

Zero LLM. Evaluates frozen run_conditions on review/protection polls.
Any break → one-way revert to FLATTEN_AT_DTE (or immediate exit if inside DTE).
Re-grant of RUN_TO_EXPIRY after revert is refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, unique

from trading.analytics.invalidation import evaluate_condition
from trading.domain.contracts.position import ExitPolicy
from trading.domain.contracts.terminal_policy import (
    DEFAULT_FLATTEN_DTE,
    TerminalPolicy,
)
from trading.domain.enums import (
    InvalidationMetric,
    InvalidationStatus,
    TerminalPolicyKind,
)

__all__ = [
    "TerminalEnforcementOutcome",
    "TerminalEnforcementResult",
    "attempt_regrant_run_to_expiry",
    "enforce_terminal_run_conditions",
    "refuse_regrant_run_to_expiry",
    "revert_to_flatten_at_dte",
]


@unique
class TerminalEnforcementOutcome(StrEnum):
    """Closed outcomes of one enforcement pass."""

    NO_TERMINAL_POLICY = "NO_TERMINAL_POLICY"
    NOT_RUN_TO_EXPIRY = "NOT_RUN_TO_EXPIRY"
    CONTINUE = "CONTINUE"
    REVERTED_TO_FLATTEN = "REVERTED_TO_FLATTEN"
    EXIT_INSIDE_DTE = "EXIT_INSIDE_DTE"


@dataclass(frozen=True, slots=True)
class TerminalEnforcementResult:
    """Pure enforcement result. Caller applies updated_policy to the position."""

    outcome: TerminalEnforcementOutcome
    updated_policy: ExitPolicy | None
    broken_condition_ids: tuple[str, ...]
    detail: str

    @property
    def requires_exit(self) -> bool:
        return self.outcome is TerminalEnforcementOutcome.EXIT_INSIDE_DTE

    @property
    def reverted(self) -> bool:
        return self.outcome in {
            TerminalEnforcementOutcome.REVERTED_TO_FLATTEN,
            TerminalEnforcementOutcome.EXIT_INSIDE_DTE,
        }


def enforce_terminal_run_conditions(
    exit_policy: ExitPolicy,
    *,
    observations: Mapping[InvalidationMetric, Decimal | str],
    days_to_expiry: int | None,
    previous: Mapping[InvalidationMetric, Decimal | str] | None = None,
    missing_observation_breaks: bool = True,
) -> TerminalEnforcementResult:
    """Evaluate frozen run_conditions; revert one-way on any break.

    missing_observation_breaks defaults True (fail-closed for terminal risk).
    """
    terminal = exit_policy.terminal_policy
    if terminal is None:
        return TerminalEnforcementResult(
            outcome=TerminalEnforcementOutcome.NO_TERMINAL_POLICY,
            updated_policy=None,
            broken_condition_ids=(),
            detail="no frozen terminal_policy",
        )
    if terminal.kind is not TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK:
        return TerminalEnforcementResult(
            outcome=TerminalEnforcementOutcome.NOT_RUN_TO_EXPIRY,
            updated_policy=None,
            broken_condition_ids=(),
            detail=f"terminal kind is {terminal.kind.value}; nothing to enforce",
        )

    broken: list[str] = []
    for condition in terminal.run_conditions:
        evaluation = evaluate_condition(
            condition, observations=observations, previous=previous
        )
        if evaluation.status is InvalidationStatus.TRIGGERED or (
            missing_observation_breaks
            and evaluation.status is InvalidationStatus.MISSING_OBSERVATION
        ):
            broken.append(condition.condition_id)

    if not broken:
        return TerminalEnforcementResult(
            outcome=TerminalEnforcementOutcome.CONTINUE,
            updated_policy=None,
            broken_condition_ids=(),
            detail="all run_conditions hold",
        )

    flatten_dte = _resolve_flatten_dte(exit_policy, terminal)
    reverted_exit = revert_to_flatten_at_dte(exit_policy, flatten_dte=flatten_dte)
    if days_to_expiry is not None and days_to_expiry <= flatten_dte:
        return TerminalEnforcementResult(
            outcome=TerminalEnforcementOutcome.EXIT_INSIDE_DTE,
            updated_policy=reverted_exit,
            broken_condition_ids=tuple(broken),
            detail=(
                f"run_conditions broken {broken}; already inside flatten DTE "
                f"{flatten_dte} (dte={days_to_expiry}); exit now"
            ),
        )
    return TerminalEnforcementResult(
        outcome=TerminalEnforcementOutcome.REVERTED_TO_FLATTEN,
        updated_policy=reverted_exit,
        broken_condition_ids=tuple(broken),
        detail=(
            f"run_conditions broken {broken}; reverted to FLATTEN_AT_DTE {flatten_dte}"
        ),
    )


def revert_to_flatten_at_dte(
    exit_policy: ExitPolicy, *, flatten_dte: int
) -> ExitPolicy:
    """One-way door: replace RUN_TO_EXPIRY with FLATTEN_AT_DTE on ExitPolicy."""
    terminal = exit_policy.terminal_policy
    if terminal is None:
        raise ValueError("cannot revert: ExitPolicy has no terminal_policy")
    if terminal.kind is TerminalPolicyKind.FLATTEN_AT_DTE:
        # Already flattened; still sync exit_before_expiry_days.
        updated_terminal = terminal
    else:
        updated_terminal = terminal.model_copy(
            update={
                "kind": TerminalPolicyKind.FLATTEN_AT_DTE,
                "flatten_dte": flatten_dte,
                "run_conditions": (),
            }
        )
    return exit_policy.model_copy(
        update={
            "terminal_policy": updated_terminal,
            "exit_before_expiry_days": flatten_dte,
        }
    )


def refuse_regrant_run_to_expiry(exit_policy: ExitPolicy) -> None:
    """Raise when an agent/path attempts to re-grant RUN_TO_EXPIRY after revert."""
    terminal = exit_policy.terminal_policy
    if terminal is None:
        return
    reverted = (
        terminal.requested_kind is TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK
        and terminal.kind is TerminalPolicyKind.FLATTEN_AT_DTE
    )
    if reverted:
        raise ValueError(
            "one-way door: cannot re-grant RUN_TO_EXPIRY_DEFINED_RISK after revert"
        )


def _resolve_flatten_dte(exit_policy: ExitPolicy, terminal: TerminalPolicy) -> int:
    if terminal.flatten_dte is not None:
        return terminal.flatten_dte
    if exit_policy.exit_before_expiry_days is not None:
        return exit_policy.exit_before_expiry_days
    return DEFAULT_FLATTEN_DTE


def attempt_regrant_run_to_expiry(
    exit_policy: ExitPolicy, terminal_policy: TerminalPolicy
) -> ExitPolicy:
    """Agent/path re-grant entry point. Always checks the one-way door first."""
    if terminal_policy.kind is not TerminalPolicyKind.RUN_TO_EXPIRY_DEFINED_RISK:
        raise ValueError("attempt_regrant_run_to_expiry requires RUN_TO_EXPIRY kind")
    if exit_policy.trade_id != terminal_policy.trade_id:
        raise ValueError("trade_id mismatch on re-grant attempt")
    refuse_regrant_run_to_expiry(exit_policy)
    if exit_policy.terminal_policy is not None:
        raise ValueError(
            "ExitPolicy already carries a frozen terminal_policy; refuse replace"
        )
    return exit_policy.model_copy(update={"terminal_policy": terminal_policy})
