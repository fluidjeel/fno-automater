"""Tests for automated counterfactual P&L evaluation engine."""

from __future__ import annotations

from decimal import Decimal

from trading.analytics.counterfactual import (
    CounterfactualTradeInput,
    evaluate_counterfactuals,
    format_counterfactual_report,
)
from trading.domain.enums import AgentAction


def test_counterfactual_empty() -> None:
    res = evaluate_counterfactuals(())
    assert res.total_evaluated == 0
    assert res.net_agent_alpha_r == Decimal("0")
    assert res.veto_count == 0


def test_counterfactual_veto_analysis() -> None:
    records = (
        # Good veto: baseline would have lost 1.0 R -> saved 1.0 R
        CounterfactualTradeInput(
            decision_id="d-1",
            trade_id="t-1",
            action=AgentAction.VETO_ENTRY,
            size_multiplier=Decimal("0.0"),
            baseline_outcome_r=Decimal("-1.0"),
        ),
        # Bad veto: baseline would have won 1.5 R -> missed 1.5 R
        CounterfactualTradeInput(
            decision_id="d-2",
            trade_id="t-2",
            action=AgentAction.VETO_ENTRY,
            size_multiplier=Decimal("0.0"),
            baseline_outcome_r=Decimal("1.5"),
        ),
    )

    res = evaluate_counterfactuals(records)
    assert res.veto_count == 2
    assert res.veto_saved_losses_r == Decimal("1.0")
    assert res.veto_missed_gains_r == Decimal("1.5")
    assert res.net_veto_value_r == Decimal("-0.5")
    assert res.net_agent_alpha_r == Decimal("-0.5")


def test_counterfactual_downscale_and_combined_alpha() -> None:
    records = (
        # Good veto: saved 1.2 R
        CounterfactualTradeInput(
            decision_id="d-1",
            trade_id="t-1",
            action=AgentAction.VETO_ENTRY,
            size_multiplier=Decimal("0.0"),
            baseline_outcome_r=Decimal("-1.2"),
        ),
        # Downscale on loss: 0.5x size on -1.0 R -> saved 0.5 R
        CounterfactualTradeInput(
            decision_id="d-2",
            trade_id="t-2",
            action=AgentAction.REDUCE_SIZE,
            size_multiplier=Decimal("0.5"),
            baseline_outcome_r=Decimal("-1.0"),
        ),
        # Downscale on win: 0.7x size on +1.0 R -> missed 0.3 R
        CounterfactualTradeInput(
            decision_id="d-3",
            trade_id="t-3",
            action=AgentAction.REDUCE_SIZE,
            size_multiplier=Decimal("0.7"),
            baseline_outcome_r=Decimal("1.0"),
        ),
    )

    res = evaluate_counterfactuals(records)
    assert res.total_evaluated == 3
    assert res.veto_count == 1
    assert res.net_veto_value_r == Decimal("1.2")

    assert res.downscale_count == 2
    assert res.downscale_saved_r == Decimal("0.5")
    assert res.downscale_missed_r == Decimal("0.3")
    assert res.net_downscale_value_r == Decimal("0.2")

    # Net alpha = +1.2 (veto) + +0.2 (downscale) = +1.4 R
    assert res.net_agent_alpha_r == Decimal("1.4")

    report = format_counterfactual_report(res)
    assert "POSITIVE ALPHA" in report
    assert "+1.40 R" in report
