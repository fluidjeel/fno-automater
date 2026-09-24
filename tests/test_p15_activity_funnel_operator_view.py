"""Phase P15: activity funnel and four-mode operator view."""

from __future__ import annotations

from pathlib import Path

import tests.factories as f
from trading.domain.enums import (
    ExecutionMode,
    FamilyId,
    FamilyResearchStatus,
    FunnelStage,
    ReasonCode,
    SystemState,
)
from trading.portfolio.arbitration import ArbitrationResult, ArbitrationSuppression
from trading.runtime.activity_funnel import build_activity_funnel
from trading.runtime.cycle_evidence import build_cycle_evidence
from trading.runtime.family_status import (
    build_family_operator_view,
    build_four_mode_allocations,
)
from trading.runtime.paper_runner import PaperCycleResult, PaperStrategyOutcome
from trading.runtime.paper_session import load_paper_session_config

ROOT = Path(__file__).resolve().parent.parent


def _cycle_result() -> PaperCycleResult:
    outcome = PaperStrategyOutcome(
        strategy_id="positional_long_option",
        snapshot_id="SNAP-1",
        intents=(),
        rejection_reasons=(ReasonCode.MIN_LOT_EXCEEDS_BUDGET,),
        decisions=(),
        order_events=(),
        executed=False,
        execution_mode=ExecutionMode.SHADOW,
    )
    return PaperCycleResult(
        system_state=SystemState.READY,
        outcomes=(outcome,),
        reconcile_id="REC-1",
        entries_blocked=False,
        arbitration_result=ArbitrationResult(
            approved_intents=(),
            suppressed_intents=(
                ArbitrationSuppression(
                    candidate_intent_id="INT-2",
                    candidate_mode_id=None,
                    candidate_family_id=FamilyId.long_straddle,
                    incumbent_id="TRD-1",
                    reason_code=ReasonCode.ECONOMIC_OVERLAP_SUPPRESSED,
                    detail="overlap suppressed",
                ),
            ),
            counterfactual_log=(),
        ),
    )


class TestActivityFunnel:
    def test_funnel_records_abstentions_and_portfolio_drops(self) -> None:
        funnel = build_activity_funnel(_cycle_result())
        assert funnel.suppressed_count == 1
        stages = {item.stage for item in funnel.drops}
        assert FunnelStage.BOUND_CONTRACTS in stages
        assert FunnelStage.PORTFOLIO in stages

    def test_cycle_evidence_includes_funnel(self) -> None:
        evidence = build_cycle_evidence(
            _cycle_result(),
            cycle_id="CYC-1",
            as_of=f.NOW,
        )
        assert evidence.funnel is not None
        assert evidence.funnel.suppressed_count == 1


class TestOperatorView:
    def test_four_mode_allocations_present(self) -> None:
        allocations = build_four_mode_allocations()
        assert len(allocations) == 4
        assert all("mode_id" in item for item in allocations)

    def test_family_rows_include_g1_g2_labels(self) -> None:
        session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
        rows = build_family_operator_view(session_cfg)
        calendar = next(
            row for row in rows if row.family_id == FamilyId.long_call_calendar.value
        )
        assert (
            calendar.research_status
            == FamilyResearchStatus.EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN.value
        )
        straddle = next(
            row for row in rows if row.family_id == FamilyId.long_straddle.value
        )
        assert straddle.g1_blocked is True
