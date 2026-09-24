"""Tests for breakout profitability across Deterministic and Agentic layers."""

from __future__ import annotations

from decimal import Decimal

from trading.analytics.breakout_evaluation import (
    BreakoutStudyReport,
    evaluate_agentic,
    evaluate_deterministic,
    run_full_study,
    run_scenario_study,
)
from trading.data.sample_breakouts import (
    BreakoutScenarioKind,
    generate_breakout_cases,
)
from trading.domain.enums import AgentAction


def test_generator_counts_and_types() -> None:
    """Ensure generator produces at least 30 trades with genuine and fake breakouts."""
    for kind in (
        BreakoutScenarioKind.POSITIONAL,
        BreakoutScenarioKind.DIRECTIONAL,
        BreakoutScenarioKind.CAS,
    ):
        cases = generate_breakout_cases(kind, count=35, seed=123)
        assert len(cases) == 35
        fakes = [c for c in cases if c.is_fake]
        genuines = [c for c in cases if not c.is_fake]

        # Asserts balanced mix of traps and genuine breakouts
        assert len(fakes) >= 15
        assert len(genuines) >= 15

        for c in cases:
            assert c.scenario_kind is kind
            assert c.underlying_snapshot.snapshot_id.startswith("SNAP-UND-")
            assert len(c.candidate_snapshots) >= 1
            assert c.shortlist.eligible_for_agent


def test_deterministic_strategy_evaluations() -> None:
    """Verify each strategy correctly emits intents on valid setups."""
    cases_pos = generate_breakout_cases(
        BreakoutScenarioKind.POSITIONAL, count=5, seed=1
    )
    cases_dir = generate_breakout_cases(
        BreakoutScenarioKind.DIRECTIONAL, count=5, seed=2
    )
    cases_cas = generate_breakout_cases(BreakoutScenarioKind.CAS, count=5, seed=3)

    for case in cases_pos:
        out = evaluate_deterministic(case)
        assert out.entered is True
        assert out.strategy_id == "debit_spread"

    for case in cases_dir:
        out = evaluate_deterministic(case)
        assert out.entered is True
        assert out.strategy_id == "positional_long_option"

    for case in cases_cas:
        out = evaluate_deterministic(case)
        assert out.entered is True
        assert out.strategy_id == "cas_microstructure"


def test_agentic_layer_trap_defense() -> None:
    """Verify Agent Desk vetoes or downscales fake breakouts while keeping winners."""
    cases = generate_breakout_cases(BreakoutScenarioKind.POSITIONAL, count=10, seed=42)
    for case in cases:
        det_out = evaluate_deterministic(case)
        agent_out = evaluate_agentic(case, det_out)

        if not case.is_fake:
            assert agent_out.action is AgentAction.SELECT_STRIKE_CANDIDATE
            assert agent_out.size_multiplier == Decimal("1.0")
            assert agent_out.realized_pnl_r > 0
        else:
            assert agent_out.action in (AgentAction.VETO_ENTRY, AgentAction.REDUCE_SIZE)
            assert agent_out.size_multiplier < Decimal("1.0")
            assert agent_out.saved_loss_r > 0


def test_scenario_study_metrics() -> None:
    """Run scenario evaluation and verify counterfactual alpha is strictly positive."""
    cases = generate_breakout_cases(BreakoutScenarioKind.CAS, count=35, seed=99)
    pairs, summary = run_scenario_study(cases)

    assert len(pairs) == 35
    assert summary.total_cases == 35
    assert summary.det_trades == 35
    assert summary.det_losses > 0
    assert summary.agent_veto_count > 0

    # Agent desk improves win rate by filtering out fake traps
    assert summary.agent_win_rate > summary.det_win_rate
    # Agent desk improves total return in R
    assert summary.agent_total_r > summary.det_total_r
    # Agent desk strictly reduces max drawdown
    assert summary.agent_max_drawdown_r < summary.det_max_drawdown_r
    # Counterfactual net alpha is positive
    assert summary.net_alpha_r > Decimal("0.0")


def test_full_study_suite() -> None:
    """Run the complete study across all three scenarios (>= 30 trades each)."""
    report: BreakoutStudyReport = run_full_study(trades_per_scenario=30, seed=10)

    # Positional check
    assert report.positional.det_trades >= 30
    assert report.positional.agent_trades <= report.positional.det_trades
    assert report.positional.net_alpha_r > 0

    # Directional check
    assert report.directional.det_trades >= 30
    assert report.directional.agent_trades <= report.directional.det_trades
    assert report.directional.net_alpha_r > 0

    # CAS check
    assert report.cas.det_trades >= 30
    assert report.cas.agent_trades <= report.cas.det_trades
    assert report.cas.net_alpha_r > 0

    # Overall check
    assert report.overall.total_cases >= 90
    assert report.overall.det_trades >= 90
    assert report.overall.net_alpha_r > 0

    # Dictionary representation contains valid fields
    data = report.to_dict()
    assert "positional" in data
    assert "directional" in data
    assert "cas" in data
    assert "overall" in data
    assert data["total_trades_evaluated"] >= 90
