"""Layer 4 analytics: fill reconstruction, scorecards and eligibility."""

from trading.analytics.eligibility import evaluate_eligibility
from trading.analytics.fills import simulate_fill, simulate_legs
from trading.analytics.scorecard import EvaluationError, build_scorecard

__all__ = [
    "EvaluationError",
    "build_scorecard",
    "evaluate_eligibility",
    "simulate_fill",
    "simulate_legs",
]
