"""DISC-A11: DISCOVERY cohort identity is never promotion evidence."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import tests.factories as f
from trading.analytics.cohort_evidence import refuse_mixed_evidence_profiles
from trading.analytics.eligibility import (
    DISCOVERY_INELIGIBLE_REASON,
    evaluate_eligibility,
)
from trading.analytics.scorecard import EvaluationError, build_scorecard
from trading.cli import main
from trading.config import load_evaluation_config
from trading.config.discovery import (
    DISCOVERY_EXPERIMENT_PREFIX,
    discovery_config_fingerprint,
    load_discovery_config,
    load_discovery_config_text,
)
from trading.config.evaluation import discovery_fill_models
from trading.domain.contracts import CohortPackage
from trading.domain.enums import EligibilityStatus, EntryProfile
from trading.runtime.cohort import experiment_id_for
from trading.runtime.paper_session import PaperSessionConfig
from trading.runtime.startup_validation import validate_startup_configuration

ROOT = Path(__file__).resolve().parent.parent
DISCOVERY_PATH = ROOT / "config" / "discovery.yaml"
PIPELINE = ROOT / "tests" / "fixtures" / "l4_cohort" / "evaluation_pipeline.yaml"
NOW = datetime.fromisoformat("2026-09-22T05:00:00+00:00")


def _discovery_experiment_id(loaded_text: str) -> str:
    loaded = load_discovery_config_text(loaded_text, source="test")
    fingerprint = discovery_config_fingerprint(
        loaded.config.profile_version,
        loaded.checksum,
    )
    return experiment_id_for(
        DISCOVERY_EXPERIMENT_PREFIX,
        "M2_DIRECTIONAL:long_option",
        NOW,
        discovery_fingerprint=fingerprint,
    )


class TestDiscoveryExperimentIdentity:
    def test_discovery_yaml_change_mints_new_experiment_id(
        self, tmp_path: Path
    ) -> None:
        base_yaml = DISCOVERY_PATH.read_text(encoding="utf-8")
        first_id = _discovery_experiment_id(base_yaml)
        changed_yaml = base_yaml.replace(
            'bug_guard_trade_risk_fraction: "0.10"',
            'bug_guard_trade_risk_fraction: "0.11"',
        )
        second_id = _discovery_experiment_id(changed_yaml)
        assert first_id.startswith("EXP-DISC-")
        assert second_id.startswith("EXP-DISC-")
        assert first_id != second_id

    def test_startup_validation_sets_exp_disc_prefix(self) -> None:
        cfg = PaperSessionConfig.model_validate(
            {
                "entry_profile": EntryProfile.DISCOVERY.value,
                "poll_interval_seconds": 60,
                "eod_local": "15:40",
                "option_strikes_each_side": 2,
                "experiment_prefix": "EXP-4M",
                "commodity_underlying": "CRUDEOIL",
                "commodity_exchange": "MCX",
                "commodity_segment": "MCX_COM",
                "cohort_dir": "data/paper/cohorts",
                "store_path": "data/paper/trading.sqlite",
                "broker_state_path": "data/paper/broker_state.json",
            }
        )
        validated, _warnings = validate_startup_configuration(
            cfg,
            discovery_path=DISCOVERY_PATH,
        )
        assert validated.experiment_prefix == DISCOVERY_EXPERIMENT_PREFIX


class TestDiscoveryEligibility:
    def _discovery_cohort_package(self) -> CohortPackage:
        loaded = load_discovery_config(DISCOVERY_PATH)
        fingerprint = discovery_config_fingerprint(
            loaded.config.profile_version,
            loaded.checksum,
        )
        experiment_id = experiment_id_for(
            DISCOVERY_EXPERIMENT_PREFIX,
            "positional_long_option",
            f.long_option_cohort_package().observation_start,
            discovery_fingerprint=fingerprint,
        )
        package = f.long_option_cohort_package()
        return package.model_copy(
            update={
                "experiment": package.experiment.model_copy(
                    update={
                        "experiment_id": experiment_id,
                        "fill_model_version": loaded.config.fills.model,
                    }
                ),
                "signals": tuple(
                    signal.model_copy(
                        update={
                            "intent": (
                                None
                                if signal.intent is None
                                else signal.intent.model_copy(
                                    update={"experiment_id": experiment_id}
                                )
                            )
                        }
                    )
                    for signal in package.signals
                ),
            }
        )

    def test_discovery_cohort_is_always_ineligible(self) -> None:
        package = self._discovery_cohort_package()
        evaluation = load_evaluation_config(PIPELINE)
        touch, _shadow = discovery_fill_models(
            evaluation.config.fill_model,
            model=package.experiment.fill_model_version,
            shadow_model="conservative-v1",
        )
        scorecard = build_scorecard(package, touch, as_of=package.observation_end)
        result = evaluate_eligibility(
            scorecard,
            evaluation.config.eligibility,
            evaluated_at=package.observation_end,
            threshold_checksum=evaluation.checksum,
        )
        assert result.status is EligibilityStatus.INELIGIBLE
        assert result.detail == DISCOVERY_INELIGIBLE_REASON
        assert result.failed_gates == ("discovery_cohort",)

    def test_cli_eligibility_for_exp_disc_cohort(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        package = self._discovery_cohort_package()
        cohort_path = tmp_path / f"{package.experiment.experiment_id}.json"
        cohort_path.write_text(package.model_dump_json(), encoding="utf-8")
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
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == EligibilityStatus.INELIGIBLE.value
        assert payload["detail"] == DISCOVERY_INELIGIBLE_REASON


class TestDiscoveryScorecardMixing:
    def test_refuses_mixed_discovery_and_strict_cohorts(self) -> None:
        strict = f.long_option_cohort_package()
        discovery = strict.model_copy(
            update={
                "experiment": strict.experiment.model_copy(
                    update={
                        "experiment_id": "EXP-DISC-LO-2026W38-deadbeef",
                        "fill_model_version": "touch-v1",
                    }
                )
            }
        )
        with pytest.raises(ValueError, match="do not mix DISCOVERY and STRICT"):
            refuse_mixed_evidence_profiles((strict, discovery))

    def test_refuses_scoring_discovery_with_strict_fill_policy(self) -> None:
        strict = f.long_option_cohort_package()
        discovery = strict.model_copy(
            update={
                "experiment": strict.experiment.model_copy(
                    update={
                        "experiment_id": "EXP-DISC-LO-2026W38-deadbeef",
                        "fill_model_version": "touch-v1",
                    }
                )
            }
        )
        conservative = load_evaluation_config(PIPELINE).config.fill_model
        with pytest.raises(EvaluationError, match="do not mix DISCOVERY and STRICT"):
            build_scorecard(discovery, conservative, as_of=discovery.observation_end)
