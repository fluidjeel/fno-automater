"""Fail-closed promotion eligibility and read-only CLI (L4-004).

Invariant 22: an eligibility report cannot deploy configuration.
An ELIGIBLE result on the offline fixture is a pipeline test, not a go-live.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from trading.analytics.eligibility import evaluate_eligibility
from trading.analytics.scorecard import build_scorecard
from trading.cli import main
from trading.config import load_evaluation_config
from trading.domain.enums import EligibilityStatus

ROOT = Path(__file__).resolve().parent.parent
SHIPPED = ROOT / "config" / "evaluation.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "l4_cohort"
PIPELINE = FIXTURES / "evaluation_pipeline.yaml"


class TestEligibility:
    def test_shipped_thresholds_are_insufficient_sample(self) -> None:
        package = f.long_option_cohort_package()
        loaded = load_evaluation_config(SHIPPED)
        scorecard = build_scorecard(
            package, loaded.config.fill_model, as_of=package.observation_end
        )
        result = evaluate_eligibility(
            scorecard,
            loaded.config.eligibility,
            evaluated_at=package.observation_end,
            threshold_checksum=loaded.checksum,
        )
        assert result.status is EligibilityStatus.INSUFFICIENT_SAMPLE
        assert "min_signals" in result.failed_gates
        assert "min_closed_trades" in result.failed_gates

    def test_unconfirmed_costs_are_ineligible_even_with_gross_profit(self) -> None:
        package = f.long_option_cohort_package()
        shipped = load_evaluation_config(SHIPPED)
        # Gate must still fail closed when charges are deliberately unverified,
        # even if the shipped evaluation.yaml now carries a verified schedule.
        unverified_fill = shipped.config.fill_model.model_copy(
            update={
                "charges_per_lot": shipped.config.fill_model.charges_per_lot.model_copy(
                    update={"value": None, "verified_at": None}
                )
            }
        )
        scorecard = build_scorecard(
            package, unverified_fill, as_of=package.observation_end
        )
        assert scorecard.gross_pnl.amount > 0
        assert scorecard.win_count >= 1
        assert scorecard.costs_confirmed is False
        loose = shipped.config.eligibility.model_copy(
            update={
                "min_observation_days": 0,
                "min_signals": 1,
                "min_closed_trades": 1,
                "min_regime_count": 0,
                "max_single_trade_pnl_share": Decimal("1"),
            }
        )
        result = evaluate_eligibility(
            scorecard,
            loose,
            evaluated_at=package.observation_end,
            threshold_checksum=shipped.checksum,
        )
        assert result.status is EligibilityStatus.INELIGIBLE
        assert "require_confirmed_costs" in result.failed_gates
        assert "min_expectancy" in result.failed_gates
        assert "win_rate" not in result.failed_gates
        assert "gross_pnl" not in result.failed_gates

    def test_pipeline_fixture_can_be_eligible(self) -> None:
        """ELIGIBLE here only proves the gate machinery. It is not a go-live."""
        package = f.long_option_cohort_package()
        loaded = load_evaluation_config(PIPELINE)
        scorecard = build_scorecard(
            package, loaded.config.fill_model, as_of=package.observation_end
        )
        result = evaluate_eligibility(
            scorecard,
            loaded.config.eligibility,
            evaluated_at=package.observation_end,
            threshold_checksum=loaded.checksum,
        )
        assert result.status is EligibilityStatus.ELIGIBLE
        assert result.failed_gates == ()


class TestEvaluateCli:
    def test_scorecard_and_eligibility_are_read_only(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cohort_path = tmp_path / "long_option.json"
        cohort_path.write_text(
            f.long_option_cohort_package().model_dump_json(), encoding="utf-8"
        )
        before = SHIPPED.read_text(encoding="utf-8")
        assert (
            main(["evaluate", "scorecard", str(cohort_path), "--config", str(SHIPPED)])
            == 0
        )
        scorecard_out = json.loads(capsys.readouterr().out)
        assert scorecard_out["experiment_id"] == "EXP-LO-PAPER-1"
        assert (
            main(
                [
                    "evaluate",
                    "eligibility",
                    str(cohort_path),
                    "--config",
                    str(PIPELINE),
                ]
            )
            == 0
        )
        eligibility_out = json.loads(capsys.readouterr().out)
        assert eligibility_out["status"] == EligibilityStatus.ELIGIBLE
        assert SHIPPED.read_text(encoding="utf-8") == before
        assert "deploy" not in eligibility_out
        assert "config_path" not in eligibility_out
