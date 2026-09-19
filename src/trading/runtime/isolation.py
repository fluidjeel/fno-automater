"""PAPER environment isolation. Live transaction adapters never reach this path."""

from __future__ import annotations

from trading.config import EvaluationConfigError, assert_execution_mode_allowed
from trading.config.schema import Environment
from trading.domain.enums import ExecutionMode

__all__ = ["PaperIsolationError", "assert_paper_isolation"]

_LIVE_BROKER_MODULES = ("trading.broker.fyers",)


class PaperIsolationError(RuntimeError):
    """PAPER process attempted to use a live transaction path."""


def assert_paper_isolation(
    environment: Environment,
    execution_mode: ExecutionMode,
    broker: object,
) -> None:
    """Refuse LIVE process env, real-capital modes, and Fyers transaction adapters."""
    if environment is not Environment.PAPER:
        raise PaperIsolationError(
            f"paper runner requires Environment.PAPER, got {environment}"
        )
    try:
        assert_execution_mode_allowed(environment, execution_mode)
    except EvaluationConfigError as exc:
        raise PaperIsolationError(str(exc)) from exc
    if execution_mode.touches_real_capital:
        raise PaperIsolationError(
            f"{execution_mode} touches real capital and cannot run in PAPER"
        )
    module = type(broker).__module__
    if any(
        module == prefix or module.startswith(f"{prefix}.")
        for prefix in _LIVE_BROKER_MODULES
    ):
        raise PaperIsolationError(
            f"{type(broker).__name__} is a live transaction adapter; PAPER "
            "may submit only through the paper broker"
        )
