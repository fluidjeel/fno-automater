"""ADESK-A3: TradeThesis falsifiability + deterministic invalidation evaluator."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading.analytics.invalidation import compare, evaluate_condition, evaluate_thesis
from trading.domain.contracts.identification import ConfidenceKind
from trading.domain.contracts.trade_thesis import InvalidationCondition, TradeThesis
from trading.domain.enums import (
    Comparator,
    DeskRole,
    DirectionalClaim,
    DriverCode,
    InvalidationMetric,
    InvalidationSeverity,
    InvalidationStatus,
)

NOW = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)


def _cond(
    condition_id: str,
    metric: InvalidationMetric,
    comparator: Comparator,
    threshold: str,
    *,
    severity: InvalidationSeverity = InvalidationSeverity.HARD,
) -> InvalidationCondition:
    return InvalidationCondition(
        condition_id=condition_id,
        metric=metric,
        comparator=comparator,
        threshold=Decimal(threshold),
        severity=severity,
    )


def _thesis(
    *extra: InvalidationCondition,
    contradicting: tuple[str, ...] = ("BEAR_CASE_NAMED",),
) -> TradeThesis:
    base = (
        _cond("c1", InvalidationMetric.SPOT_PCT_FROM_ENTRY, Comparator.LTE, "-2"),
        _cond("c2", InvalidationMetric.IV_PERCENTILE, Comparator.GTE, "80"),
    )
    invalidation = base + extra
    return TradeThesis(
        thesis_id="th-1",
        trade_id="tr-1",
        snapshot_id="snap-1",
        written_at=NOW,
        author=DeskRole.ENTRY,
        model_id="test-model",
        prompt_version="p1",
        directional_claim=DirectionalClaim.BULLISH,
        horizon_days=5,
        primary_driver=DriverCode.TREND_CONTINUATION,
        supporting_reason_codes=("TREND_UP",),
        contradicting_reason_codes=contradicting,
        invalidation=invalidation,
        confidence=Decimal("0.6"),
        confidence_kind=ConfidenceKind.RAW_SCORE,
        thesis_hash="abc123",
    )


def test_thesis_requires_contradicting_reason() -> None:
    with pytest.raises(ValidationError, match="contradicting"):
        _thesis(contradicting=())


def test_thesis_requires_two_invalidations() -> None:
    with pytest.raises(ValidationError, match="two invalidation"):
        TradeThesis(
            thesis_id="th-1",
            trade_id="tr-1",
            snapshot_id="snap-1",
            written_at=NOW,
            author=DeskRole.ENTRY,
            model_id="m",
            prompt_version="p",
            directional_claim=DirectionalClaim.BEARISH,
            horizon_days=2,
            primary_driver=DriverCode.MEAN_REVERSION,
            contradicting_reason_codes=("X",),
            invalidation=(_cond("only", InvalidationMetric.DTE, Comparator.LTE, "1"),),
            confidence=Decimal("0.5"),
            confidence_kind=ConfidenceKind.RAW_SCORE,
            thesis_hash="h",
        )


@pytest.mark.parametrize(
    ("comparator", "observed", "threshold", "previous", "expected"),
    [
        (Comparator.LT, "1", "2", None, True),
        (Comparator.LTE, "2", "2", None, True),
        (Comparator.GT, "3", "2", None, True),
        (Comparator.GTE, "2", "2", None, True),
        (Comparator.EQ, "2", "2", None, True),
        (Comparator.CROSSES_BELOW, "1.5", "2", "2.5", True),
        (Comparator.CROSSES_ABOVE, "2.5", "2", "1.5", True),
        (Comparator.CROSSES_BELOW, "2.5", "2", "2.5", False),
    ],
)
def test_compare_matrix(
    comparator: Comparator,
    observed: str,
    threshold: str,
    previous: str | None,
    expected: bool,
) -> None:
    assert (
        compare(
            comparator=comparator,
            observed=Decimal(observed),
            threshold=Decimal(threshold),
            previous=None if previous is None else Decimal(previous),
        )
        is expected
    )


@pytest.mark.parametrize(
    ("metric", "comparator", "threshold", "value", "should_fire"),
    [
        (InvalidationMetric.SPOT_PCT_FROM_ENTRY, Comparator.LTE, "-2", "-2.5", True),
        (InvalidationMetric.IV_PERCENTILE, Comparator.GTE, "80", "81", True),
        (InvalidationMetric.TREND_SCORE, Comparator.LTE, "0", "-0.2", True),
        (InvalidationMetric.OI_CHANGE_PCT, Comparator.LTE, "-10", "-12", True),
        (InvalidationMetric.ATR_MULTIPLE, Comparator.GTE, "2", "2.1", True),
        (InvalidationMetric.DTE, Comparator.LTE, "1", "1", True),
        (InvalidationMetric.MAE_R, Comparator.GTE, "1", "1.2", True),
        (InvalidationMetric.DELTA, Comparator.LTE, "0.2", "0.15", True),
        (InvalidationMetric.VEGA_PNL_R, Comparator.LTE, "-0.5", "-0.6", True),
        (InvalidationMetric.EVENT_RISK_STATE, Comparator.GTE, "1", "1", True),
        (InvalidationMetric.REALIZED_VOL_RATIO, Comparator.GTE, "1.5", "1.6", True),
        (InvalidationMetric.INDIA_VIX, Comparator.GTE, "20", "21", True),
    ],
)
def test_every_invalidation_metric_triggers(
    metric: InvalidationMetric,
    comparator: Comparator,
    threshold: str,
    value: str,
    should_fire: bool,
) -> None:
    """Golden path: each InvalidationMetric can fire through the evaluator."""
    condition = _cond("m", metric, comparator, threshold)
    result = evaluate_condition(
        condition,
        observations={metric: Decimal(value)},
    )
    assert result.triggered is should_fire
    assert result.status is InvalidationStatus.TRIGGERED


def test_missing_observation_is_not_triggered() -> None:
    condition = _cond("m", InvalidationMetric.INDIA_VIX, Comparator.GTE, "20")
    result = evaluate_condition(condition, observations={})
    assert result.status is InvalidationStatus.MISSING_OBSERVATION
    assert result.triggered is False


def test_crosses_requires_previous() -> None:
    condition = _cond(
        "x",
        InvalidationMetric.SPOT_PCT_FROM_ENTRY,
        Comparator.CROSSES_BELOW,
        "0",
    )
    result = evaluate_condition(
        condition,
        observations={InvalidationMetric.SPOT_PCT_FROM_ENTRY: Decimal("-0.1")},
    )
    assert result.status is InvalidationStatus.MISSING_OBSERVATION


def test_thesis_hard_break() -> None:
    thesis = _thesis()
    evaluation = evaluate_thesis(
        thesis,
        observations={
            InvalidationMetric.SPOT_PCT_FROM_ENTRY: Decimal("-3"),
            InvalidationMetric.IV_PERCENTILE: Decimal("50"),
        },
    )
    assert evaluation.thesis_broken is True
    assert "c1" in evaluation.hard_triggered


def test_thesis_soft_only_does_not_break() -> None:
    thesis = TradeThesis(
        thesis_id="th-2",
        trade_id="tr-2",
        snapshot_id="snap-2",
        written_at=NOW,
        author=DeskRole.POSITION,
        model_id="m",
        prompt_version="p",
        directional_claim=DirectionalClaim.BULLISH,
        horizon_days=3,
        primary_driver=DriverCode.FLOW_IMBALANCE,
        contradicting_reason_codes=("FADE_RISK",),
        invalidation=(
            _cond(
                "soft",
                InvalidationMetric.IV_PERCENTILE,
                Comparator.GTE,
                "70",
                severity=InvalidationSeverity.SOFT,
            ),
            _cond(
                "hard",
                InvalidationMetric.SPOT_PCT_FROM_ENTRY,
                Comparator.LTE,
                "-5",
                severity=InvalidationSeverity.HARD,
            ),
        ),
        confidence=Decimal("0.55"),
        confidence_kind=ConfidenceKind.RAW_SCORE,
        thesis_hash="h2",
    )
    evaluation = evaluate_thesis(
        thesis,
        observations={
            InvalidationMetric.IV_PERCENTILE: Decimal("75"),
            InvalidationMetric.SPOT_PCT_FROM_ENTRY: Decimal("-1"),
        },
    )
    assert evaluation.thesis_broken is False
    assert evaluation.soft_triggered == ("soft",)
