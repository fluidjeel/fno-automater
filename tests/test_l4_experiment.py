"""Layer 4 experiment identity and frozen-exit lineage (L4-001).

Invariant 2: Layer 4 types carry no broker, order or promotion levers.
Invariant 22: evaluator output cannot deploy configuration.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import tests.factories as f
from trading.config import (
    Environment,
    EvaluationConfigError,
    assert_execution_mode_allowed,
)
from trading.domain.enums import ExecutionMode
from trading.domain.primitives import Money


class TestExperimentDefinition:
    def test_paper_experiment_round_trips(self) -> None:
        original = f.experiment()
        restored = type(original).model_validate(original.model_dump(mode="json"))
        assert restored == original
        assert restored.parameters_frozen is True
        assert restored.execution_mode is ExecutionMode.PAPER

    def test_experiment_cannot_unfreeze_after_start(self) -> None:
        """A parameter change requires a new experiment_id, not an unfreeze."""
        with pytest.raises(ValidationError, match="frozen after start"):
            f.experiment(parameters_frozen=False)
        frozen = f.experiment()
        with pytest.raises(ValidationError, match="frozen after start"):
            type(frozen).model_validate(
                {**frozen.model_dump(mode="json"), "parameters_frozen": False}
            )

    def test_shadow_requires_zero_capital(self) -> None:
        with pytest.raises(ValidationError, match="zero capital_limit"):
            f.experiment(
                execution_mode=ExecutionMode.SHADOW,
                capital_limit=f.money("1"),
            )
        shadow = f.experiment(
            execution_mode=ExecutionMode.SHADOW,
            capital_limit=Money.zero(f.INR),
        )
        assert shadow.capital_limit.is_zero

    def test_real_modes_require_positive_capital(self) -> None:
        with pytest.raises(ValidationError, match="positive capital_limit"):
            f.experiment(
                execution_mode=ExecutionMode.CANARY_REAL,
                capital_limit=Money.zero(f.INR),
            )
        canary = f.experiment(
            execution_mode=ExecutionMode.CANARY_REAL,
            capital_limit=f.money("50000"),
        )
        assert canary.execution_mode.touches_real_capital is True

    def test_real_modes_require_live_process_environment(self) -> None:
        assert_execution_mode_allowed(Environment.LIVE, ExecutionMode.CANARY_REAL)
        assert_execution_mode_allowed(Environment.PAPER, ExecutionMode.PAPER)
        with pytest.raises(EvaluationConfigError, match="LIVE"):
            assert_execution_mode_allowed(Environment.PAPER, ExecutionMode.CANARY_REAL)
        with pytest.raises(EvaluationConfigError, match="LIVE"):
            assert_execution_mode_allowed(
                Environment.BACKTEST, ExecutionMode.NORMAL_REAL
            )


class TestLineageStamps:
    def test_intent_and_order_identity_carry_experiment_lineage(self) -> None:
        intent = f.intent(
            experiment_id="EXP-V1",
            execution_mode=ExecutionMode.PAPER,
            strategy_version="long-option-v1",
        )
        identity = f.order_identity(
            experiment_id=intent.experiment_id,
            execution_mode=intent.execution_mode,
            intent_id=intent.intent_id,
        )
        decision = f.risk_decision(
            experiment_id=intent.experiment_id,
            execution_mode=intent.execution_mode,
            intent_id=intent.intent_id,
        )
        assert identity.experiment_id == "EXP-V1"
        assert identity.execution_mode is ExecutionMode.PAPER
        assert decision.experiment_id == intent.experiment_id
        assert decision.execution_mode is intent.execution_mode

    def test_open_position_keeps_opening_version_and_exit_policy(self) -> None:
        """A position opened under V keeps V's exit policy; new config is new trades."""
        opening = f.position_state(
            strategy_version="long-option-v1",
            experiment_id="EXP-V1",
            execution_mode=ExecutionMode.PAPER,
            exit_policy=f.exit_policy(
                initial_stop_distance_ticks=200,
                current_stop_distance_ticks=200,
            ),
        )
        newer = f.intent(
            strategy_version="long-option-v2",
            experiment_id="EXP-V2",
            exit_template=f.exit_template(stop_distance_ticks=100),
        )
        assert opening.strategy_version == "long-option-v1"
        assert opening.exit_policy.initial_stop_distance_ticks == 200
        assert newer.exit_template.stop_distance_ticks == 100
        with pytest.raises(ValidationError, match="frozen"):
            opening.strategy_version = "long-option-v2"
        with pytest.raises(ValidationError, match="frozen"):
            opening.experiment_id = newer.experiment_id
        with pytest.raises(ValidationError, match="exceeds"):
            type(opening.exit_policy).model_validate(
                {
                    **opening.exit_policy.model_dump(mode="json"),
                    "current_stop_distance_ticks": 400,
                }
            )
