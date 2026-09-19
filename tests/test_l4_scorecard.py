"""Deterministic scorecard over a frozen long-option cohort (L4-003)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import tests.factories as f
from trading.analytics.scorecard import EvaluationError, build_scorecard
from trading.config import load_evaluation_config
from trading.domain.contracts import CohortPackage
from trading.domain.enums import ReasonCode

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "config" / "evaluation.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "l4_cohort"
PIPELINE = FIXTURES / "evaluation_pipeline.yaml"
COHORT_JSON = FIXTURES / "long_option.json"


class TestScorecard:
    def test_fixture_includes_declines_and_rejects(self) -> None:
        package = f.long_option_cohort_package()
        reasons = {signal.rejection_reason for signal in package.signals}
        assert ReasonCode.SPREAD_TOO_WIDE in reasons
        assert ReasonCode.RISK_LIMIT_TRADE in reasons
        assert any(signal.declined for signal in package.signals)
        assert package.experiment.parameters_frozen is True

    def test_committed_json_fixture_matches_factory_cohort(self) -> None:
        package = f.long_option_cohort_package()
        loaded = CohortPackage.model_validate_json(
            COHORT_JSON.read_text(encoding="utf-8")
        )
        assert loaded.experiment.experiment_id == package.experiment.experiment_id
        assert len(loaded.signals) == len(package.signals)
        assert {signal.signal_id for signal in loaded.signals} == {
            signal.signal_id for signal in package.signals
        }

    def test_scorecard_counts_closed_trades_and_rejects(self) -> None:
        package = f.long_option_cohort_package()
        policy = load_evaluation_config(PIPELINE).config.fill_model
        scorecard = build_scorecard(package, policy, as_of=package.observation_end)
        assert scorecard.signal_count == 4
        assert scorecard.declined_count == 1
        assert scorecard.closed_trade_count == 2
        assert scorecard.filled_count == 2
        assert scorecard.win_count == 1
        assert scorecard.loss_count == 1
        assert scorecard.regime_count == 2
        assert scorecard.costs_confirmed is True
        assert scorecard.net_pnl is not None
        assert scorecard.expectancy is not None
        histogram = {row.reason_code: row.count for row in scorecard.reason_histogram}
        assert histogram[ReasonCode.SPREAD_TOO_WIDE] == 1
        assert histogram[ReasonCode.RISK_LIMIT_TRADE] == 1

    def test_unverified_charges_leave_net_pnl_unset(self) -> None:
        package = f.long_option_cohort_package()
        policy = load_evaluation_config(SHIPPED).config.fill_model
        scorecard = build_scorecard(package, policy, as_of=package.observation_end)
        assert scorecard.costs_confirmed is False
        assert scorecard.net_pnl is None
        assert scorecard.expectancy is None
        assert scorecard.gross_pnl.amount != 0

    def test_refuses_to_pool_fill_model_versions(self) -> None:
        package = f.long_option_cohort_package()
        policy = load_evaluation_config(PIPELINE).config.fill_model
        mismatched = policy.model_copy(update={"version": "conservative-v2"})
        with pytest.raises(EvaluationError, match="do not pool versions"):
            build_scorecard(package, mismatched, as_of=package.observation_end)

    def test_refuses_mixed_experiment_ids_in_the_package(self) -> None:
        package = f.long_option_cohort_package()
        foreign = package.signals[0].model_copy(
            update={
                "intent": f.intent(
                    experiment_id="EXP-OTHER",
                    execution_mode=package.experiment.execution_mode,
                )
            }
        )
        with pytest.raises(ValidationError, match="does not match the package"):
            type(package)(
                experiment=package.experiment,
                signals=(foreign, *package.signals[1:]),
                observation_start=package.observation_start,
                observation_end=package.observation_end,
            )
