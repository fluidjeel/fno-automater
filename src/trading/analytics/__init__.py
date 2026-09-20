"""Layer 4 analytics: fill reconstruction, scorecards and eligibility."""

from trading.analytics.eligibility import evaluate_eligibility
from trading.analytics.fills import simulate_fill, simulate_legs
from trading.analytics.judgment import JudgmentError, evaluate_judgment, label_signal
from trading.analytics.stress import (
    PositionStressInput,
    build_stress_entry_freeze,
    build_stress_report,
    evaluate_tail_budget,
)
from trading.analytics.invalidation import (
    evaluate_condition,
    evaluate_thesis,
)
from trading.analytics.scorecard import EvaluationError, build_scorecard

__all__ = [
    "EvaluationError",
    "JudgmentError",
    "build_scorecard",
    "evaluate_condition",
    "evaluate_eligibility",
    "evaluate_thesis",
    "PositionStressInput",
    "build_stress_entry_freeze",
    "build_stress_report",
    "evaluate_tail_budget",
    "evaluate_judgment",
    "label_signal",
    "simulate_fill",
    "simulate_legs",
]
