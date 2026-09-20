"""Deterministic invalidation evaluator. Agents write conditions; this judges them.

Pure functions only: no I/O, no LLM, no wall clock. ADESK-A3.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from trading.domain.contracts.trade_thesis import InvalidationCondition, TradeThesis
from trading.domain.enums import Comparator, InvalidationMetric, InvalidationStatus

__all__ = [
    "ConditionEvaluation",
    "ThesisEvaluation",
    "compare",
    "evaluate_condition",
    "evaluate_thesis",
]


def compare(
    *,
    comparator: Comparator,
    observed: Decimal | str,
    threshold: Decimal | str,
    previous: Decimal | str | None = None,
) -> bool:
    """Return True when the comparator relation holds for the observation."""
    left = _as_decimal(observed)
    right = _as_decimal(threshold)
    simple = {
        Comparator.LT: left < right,
        Comparator.LTE: left <= right,
        Comparator.GT: left > right,
        Comparator.GTE: left >= right,
        Comparator.EQ: left == right,
    }
    if comparator in simple:
        return simple[comparator]

    if previous is None:
        raise ValueError(f"{comparator} requires a previous observation")
    prev = _as_decimal(previous)
    if comparator is Comparator.CROSSES_BELOW:
        return prev >= right > left
    if comparator is Comparator.CROSSES_ABOVE:
        return prev <= right < left
    raise ValueError(f"unsupported comparator: {comparator}")


def evaluate_condition(
    condition: InvalidationCondition,
    *,
    observations: Mapping[InvalidationMetric, Decimal | str],
    previous: Mapping[InvalidationMetric, Decimal | str] | None = None,
) -> ConditionEvaluation:
    """Evaluate one condition against the current (and optional prior) snapshot."""
    if condition.metric not in observations:
        return ConditionEvaluation(
            condition_id=condition.condition_id,
            metric=condition.metric,
            status=InvalidationStatus.MISSING_OBSERVATION,
            triggered=False,
        )
    prior_map = previous or {}
    needs_prior = condition.comparator in {
        Comparator.CROSSES_BELOW,
        Comparator.CROSSES_ABOVE,
    }
    if needs_prior and condition.metric not in prior_map:
        return ConditionEvaluation(
            condition_id=condition.condition_id,
            metric=condition.metric,
            status=InvalidationStatus.MISSING_OBSERVATION,
            triggered=False,
        )
    fired = compare(
        comparator=condition.comparator,
        observed=observations[condition.metric],
        threshold=condition.threshold,
        previous=prior_map.get(condition.metric),
    )
    return ConditionEvaluation(
        condition_id=condition.condition_id,
        metric=condition.metric,
        status=(
            InvalidationStatus.TRIGGERED if fired else InvalidationStatus.HOLDING
        ),
        triggered=fired,
    )


def evaluate_thesis(
    thesis: TradeThesis,
    *,
    observations: Mapping[InvalidationMetric, Decimal | str],
    previous: Mapping[InvalidationMetric, Decimal | str] | None = None,
) -> ThesisEvaluation:
    """Evaluate every invalidation condition on a thesis. Never mutates the thesis."""
    results = tuple(
        evaluate_condition(cond, observations=observations, previous=previous)
        for cond in thesis.invalidation
    )
    hard_ids = tuple(
        r.condition_id
        for r, c in zip(results, thesis.invalidation, strict=True)
        if r.triggered and c.severity.value == "HARD"
    )
    soft_ids = tuple(
        r.condition_id
        for r, c in zip(results, thesis.invalidation, strict=True)
        if r.triggered and c.severity.value == "SOFT"
    )
    return ThesisEvaluation(
        thesis_id=thesis.thesis_id,
        conditions=results,
        hard_triggered=hard_ids,
        soft_triggered=soft_ids,
        thesis_broken=bool(hard_ids),
    )


def _as_decimal(value: Decimal | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class ConditionEvaluation:
    """Result for one InvalidationCondition."""

    __slots__ = ("condition_id", "metric", "status", "triggered")

    def __init__(
        self,
        *,
        condition_id: str,
        metric: InvalidationMetric,
        status: InvalidationStatus,
        triggered: bool,
    ) -> None:
        self.condition_id = condition_id
        self.metric = metric
        self.status = status
        self.triggered = triggered


class ThesisEvaluation:
    """Aggregate invalidation result for one TradeThesis."""

    __slots__ = (
        "conditions",
        "hard_triggered",
        "soft_triggered",
        "thesis_broken",
        "thesis_id",
    )

    def __init__(
        self,
        *,
        thesis_id: str,
        conditions: tuple[ConditionEvaluation, ...],
        hard_triggered: tuple[str, ...],
        soft_triggered: tuple[str, ...],
        thesis_broken: bool,
    ) -> None:
        self.thesis_id = thesis_id
        self.conditions = conditions
        self.hard_triggered = hard_triggered
        self.soft_triggered = soft_triggered
        self.thesis_broken = thesis_broken
