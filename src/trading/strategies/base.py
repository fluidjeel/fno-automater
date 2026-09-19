"""Layer 3 strategy boundary: pure, deterministic decision systems.

A strategy reads immutable inputs and returns a decision. It is a pure function
of those inputs: the same inputs must produce byte-identical outputs, so the
wall clock and any randomness are injected, never read here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from trading.domain.contracts import FeatureSnapshot, PortfolioView, TradeIntent
from trading.domain.enums import ExecutionMode, ReasonCode

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from trading.strategies.macro import MacroAssessment

__all__ = [
    "Rejection",
    "Strategy",
    "StrategyContext",
    "StrategyDecision",
    "build_strategy",
]


@dataclass(frozen=True, slots=True)
class Rejection:
    """Why a strategy declined to emit an intent, in machine-readable form."""

    reason: ReasonCode
    detail: str
    snapshot_id: str
    as_of: datetime


@dataclass(frozen=True, slots=True)
class StrategyDecision:
    """The deterministic output of one strategy evaluation."""

    strategy_id: str
    strategy_version: str
    snapshot_id: str
    as_of: datetime
    intents: tuple[TradeIntent, ...]
    rejections: tuple[Rejection, ...]

    @property
    def emits_intent(self) -> bool:
        return bool(self.intents)


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """All inputs a strategy may read for exactly one decision.

    ``underlying`` is the underlying index/stock/commodity snapshot;
    ``candidates`` are the tradable contract snapshots the strategy is asked to
    consider (one for a single-leg, two for a spread). ``now`` is the injected
    decision instant, so no strategy reads wall time.
    """

    underlying: FeatureSnapshot
    candidates: tuple[FeatureSnapshot, ...]
    view: PortfolioView
    now: datetime
    experiment_id: str = "EXP-PAPER-DEFAULT"
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    macro: MacroAssessment | None = None


class Strategy(Protocol):
    """The common strategy interface. Implementations are registered by id."""

    strategy_id: str
    strategy_version: str

    def evaluate(self, ctx: StrategyContext) -> StrategyDecision: ...


_REGISTRY: dict[str, type[Strategy]] = {}


def register_strategy(cls: type[Strategy]) -> type[Strategy]:
    """Register a strategy class under its ``strategy_id``."""
    _REGISTRY[cls.strategy_id] = cls
    return cls


def build_strategy(strategy_id: str) -> Strategy:
    """Instantiate a registered strategy by id."""
    try:
        cls = _REGISTRY[strategy_id]
    except KeyError as exc:
        raise KeyError(f"unknown strategy_id {strategy_id!r}") from exc
    return cls()
