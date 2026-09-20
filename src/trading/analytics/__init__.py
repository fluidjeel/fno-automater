"""Layer 4 analytics: fill reconstruction, scorecards and eligibility."""

from trading.analytics.bias import (
    BiasBatteryBands,
    DecisionBiasInput,
    TradeOutcomeInput,
    WarmColdPair,
    build_bias_report,
)
from trading.analytics.eligibility import evaluate_eligibility
from trading.analytics.fills import simulate_fill, simulate_legs
from trading.analytics.hypothesis_promotion import (
    cluster_to_hypothesis,
    hypothesis_to_shadow_experiment,
    promote_improvements,
)
from trading.analytics.improvements import (
    apply_stale_status,
    cluster_improvements,
    hypothesis_eligible,
    merge_duplicate,
    normalize_claim_key,
)
from trading.analytics.invalidation import (
    evaluate_condition,
    evaluate_thesis,
)
from trading.analytics.judgment import JudgmentError, evaluate_judgment, label_signal
from trading.analytics.reason_preconditions import (
    EvidenceBundle,
    ground_agent_reasons,
)
from trading.analytics.research_weekly import (
    build_research_weekly,
    persist_research_weekly,
)
from trading.analytics.scorecard import EvaluationError, build_scorecard
from trading.analytics.stress import (
    PositionStressInput,
    build_stress_entry_freeze,
    build_stress_report,
    evaluate_tail_budget,
)

__all__ = [
    "BiasBatteryBands",
    "DecisionBiasInput",
    "EvaluationError",
    "EvidenceBundle",
    "JudgmentError",
    "PositionStressInput",
    "TradeOutcomeInput",
    "WarmColdPair",
    "apply_stale_status",
    "build_bias_report",
    "build_research_weekly",
    "build_scorecard",
    "build_stress_entry_freeze",
    "build_stress_report",
    "cluster_improvements",
    "evaluate_condition",
    "evaluate_eligibility",
    "evaluate_judgment",
    "evaluate_tail_budget",
    "evaluate_thesis",
    "ground_agent_reasons",
    "hypothesis_eligible",
    "label_signal",
    "merge_duplicate",
    "normalize_claim_key",
    "persist_research_weekly",
    "simulate_fill",
    "simulate_legs",
]
