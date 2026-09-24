"""ADESK-A6: bias battery emits all 11 PART 9.3 metrics on a fixture cohort."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading.analytics.bias import (
    DecisionBiasInput,
    TradeOutcomeInput,
    WarmColdPair,
    build_bias_report,
)
from trading.domain.contracts.bias import BiasMetricId

NOW = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)


def _trade(
    i: int,
    *,
    winner: bool,
    hold: str,
    capture: str,
    pnl: str,
    cost: str,
    conf: str,
    verdict: str,
    size: str,
    weekday: int = 0,
    hour: int = 10,
    iv: str = "MID",
) -> TradeOutcomeInput:
    return TradeOutcomeInput(
        trade_id=f"t{i}",
        is_winner=winner,
        hold_minutes=Decimal(hold),
        capture_ratio=Decimal(capture),
        pnl_r=Decimal(pnl),
        execution_cost_r=Decimal(cost),
        closed_at=NOW + timedelta(hours=i),
        entry_weekday=weekday,
        entry_hour=hour,
        iv_bucket=iv,
        supporting_reason_count=2,
        contradicting_reason_count=1,
        entry_confidence=Decimal(conf),
        posttrade_thesis_verdict=Decimal(verdict),
        size_multiplier=Decimal(size),
    )


def _cohort() -> tuple[TradeOutcomeInput, ...]:
    return (
        _trade(
            0,
            winner=True,
            hold="120",
            capture="0.60",
            pnl="1.0",
            cost="0.10",
            conf="0.70",
            verdict="0.70",
            size="1.0",
        ),
        _trade(
            1,
            winner=False,
            hold="40",
            capture="0.00",
            pnl="-0.8",
            cost="0.10",
            conf="0.60",
            verdict="0.40",
            size="1.0",
        ),
        _trade(
            2,
            winner=True,
            hold="180",
            capture="0.50",
            pnl="1.2",
            cost="0.15",
            conf="0.80",
            verdict="0.75",
            size="1.1",
        ),
        _trade(
            3,
            winner=False,
            hold="30",
            capture="0.00",
            pnl="-0.5",
            cost="0.08",
            conf="0.55",
            verdict="0.30",
            size="0.9",
            weekday=2,
            hour=14,
        ),
        _trade(
            4,
            winner=True,
            hold="90",
            capture="0.70",
            pnl="0.9",
            cost="0.12",
            conf="0.65",
            verdict="0.60",
            size="1.2",
        ),
        _trade(
            5,
            winner=True,
            hold="100",
            capture="0.55",
            pnl="1.1",
            cost="0.10",
            conf="0.70",
            verdict="0.65",
            size="1.3",
        ),
        _trade(
            6,
            winner=True,
            hold="110",
            capture="0.50",
            pnl="1.0",
            cost="0.10",
            conf="0.75",
            verdict="0.70",
            size="1.4",
        ),
    )


def test_bias_battery_emits_all_eleven_metrics() -> None:
    decisions = (
        DecisionBiasInput("d1", NOW, Decimal("1.0"), Decimal("-0.5")),
        DecisionBiasInput("d2", NOW, Decimal("1.2"), Decimal("1.0")),
        DecisionBiasInput("d3", NOW, Decimal("0.8"), Decimal("-1.0")),
        DecisionBiasInput("d4", NOW, Decimal("1.1"), Decimal("0.5")),
    )
    pairs = (
        WarmColdPair("t0", "HOLD", "HOLD"),
        WarmColdPair("t1", "EXIT", "HOLD"),
    )
    report = build_bias_report(
        as_of=NOW,
        cohort_id="fixture-a6",
        trades=_cohort(),
        decisions=decisions,
        warm_cold_pairs=pairs,
    )
    assert len(report.metrics) == 11
    assert {m.metric_id for m in report.metrics} == set(BiasMetricId)
    # Disposition: winners held longer than losers → ratio > 1
    disp = report.result_for(BiasMetricId.DISPOSITION_EFFECT)
    assert disp is not None and disp.value is not None and disp.value > 1
    # Premature exit mean capture on winners
    prem = report.result_for(BiasMetricId.PREMATURE_EXIT)
    assert prem is not None and prem.value is not None
    assert report.attention_required in (True, False)


def test_empty_cohort_still_returns_eleven_rows() -> None:
    report = build_bias_report(as_of=NOW, cohort_id="empty", trades=())
    assert len(report.metrics) == 11
    assert all(m.value is None for m in report.metrics)
    assert report.attention_required is False


def test_cost_blindness_rises_with_high_costs() -> None:
    cheap = _cohort()
    expensive = tuple(
        TradeOutcomeInput(
            trade_id=t.trade_id,
            is_winner=t.is_winner,
            hold_minutes=t.hold_minutes,
            capture_ratio=t.capture_ratio,
            pnl_r=t.pnl_r,
            execution_cost_r=Decimal("0.80"),
            closed_at=t.closed_at,
            entry_weekday=t.entry_weekday,
            entry_hour=t.entry_hour,
            iv_bucket=t.iv_bucket,
            supporting_reason_count=t.supporting_reason_count,
            contradicting_reason_count=t.contradicting_reason_count,
            entry_confidence=t.entry_confidence,
            posttrade_thesis_verdict=t.posttrade_thesis_verdict,
            size_multiplier=t.size_multiplier,
        )
        for t in cheap
    )
    low = build_bias_report(as_of=NOW, cohort_id="c", trades=cheap)
    high = build_bias_report(as_of=NOW, cohort_id="e", trades=expensive)
    low_v = low.result_for(BiasMetricId.COST_BLINDNESS)
    high_v = high.result_for(BiasMetricId.COST_BLINDNESS)
    assert low_v and high_v and low_v.value is not None and high_v.value is not None
    assert high_v.value > low_v.value
