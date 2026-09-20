"""ADESK-B4: review-level labelling precision and capture."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from trading.analytics.judgment import evaluate_reviews, label_review
from trading.domain.contracts.review_judgment import ReviewJudgmentRow
from trading.domain.enums import ReviewAction

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def test_label_review_hold_better_when_subsequent_positive() -> None:
    assert label_review(action=ReviewAction.HOLD, subsequent_r=Decimal("0.5")) is True
    assert label_review(action=ReviewAction.HOLD, subsequent_r=Decimal("-0.2")) is False
    assert label_review(action=ReviewAction.HOLD, subsequent_r=None) is None


def test_evaluate_reviews_precision_and_capture() -> None:
    rows = (
        ReviewJudgmentRow(
            review_id="r1",
            trade_id="t1",
            action=ReviewAction.HOLD,
            should_hold=True,
            held=True,
            subsequent_r=Decimal("0.4"),
        ),
        ReviewJudgmentRow(
            review_id="r2",
            trade_id="t1",
            action=ReviewAction.FULL_EXIT,
            should_hold=False,
            held=False,
            subsequent_r=Decimal("-0.3"),
        ),
        ReviewJudgmentRow(
            review_id="r3",
            trade_id="t2",
            action=ReviewAction.HOLD,
            should_hold=False,
            held=True,
            subsequent_r=Decimal("-0.1"),
        ),
    )
    report = evaluate_reviews(rows, as_of=NOW)
    assert report.labeled_count == 3
    assert report.hold_correct_count == 1
    assert report.exit_correct_count == 1
    assert report.precision == Decimal("0.5000")  # 1/2 held correct
    assert report.capture == Decimal("1.0000")  # 1/1 should_hold captured
