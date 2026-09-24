"""Tests for startup configuration validation (Phase P1 / spec §4, §10.1, G1-G3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from trading.domain.contracts.mode_policy import load_modes_config
from trading.domain.enums import ExecutionMode
from trading.runtime.cas_event_path import CasEventDrivenConfig
from trading.runtime.paper_session import (
    PaperSessionConfig,
    load_paper_session_config,
)
from trading.runtime.startup_validation import (
    StartupValidationError,
    validate_startup_configuration,
)

ROOT = Path(__file__).resolve().parent.parent


def _sample_config(**overrides: object) -> PaperSessionConfig:
    payload: dict[str, object] = {
        "poll_interval_seconds": 60,
        "eod_local": "15:40",
        "option_strikes_each_side": 2,
        "experiment_prefix": "EXP-TEST",
        "strategy_ids": (
            "positional_long_option",
            "debit_spread",
            "defined_risk_multileg",
            "cas_microstructure",
            "commodity_futures_trend",
        ),
        "strategy_stances": {
            "positional_long_option": ExecutionMode.PAPER,
            "debit_spread": ExecutionMode.PAPER,
            "defined_risk_multileg": ExecutionMode.SHADOW,
            "cas_microstructure": ExecutionMode.SHADOW,
            "commodity_futures_trend": ExecutionMode.SHADOW,
        },
        "commodity_underlying": "CRUDEOIL",
        "commodity_exchange": "MCX",
        "commodity_segment": "MCX_COM",
        "cohort_dir": "data/paper/cohorts",
        "store_path": "data/paper/trading.sqlite",
        "broker_state_path": "data/paper/broker_state.json",
    }
    payload.update(overrides)
    return PaperSessionConfig.model_validate(payload)


class TestStartupValidation:
    def test_startup_validation_error_is_value_error(self) -> None:
        assert issubclass(StartupValidationError, ValueError)

    def test_valid_config_passes_cleanly(self) -> None:
        cfg = _sample_config()
        validated_cfg, warnings = validate_startup_configuration(cfg)
        assert validated_cfg == cfg
        assert warnings == []

    def test_g3_slow_poll_warns_without_demoting_cas(self) -> None:
        cfg = _sample_config(
            poll_interval_seconds=60,
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                "cas_microstructure": ExecutionMode.PAPER,
            },
            cas_event_driven=CasEventDrivenConfig(enabled=True),
        )
        validated_cfg, warnings = validate_startup_configuration(
            cfg, enforce_g3_shadow=False
        )
        assert validated_cfg.strategy_stances["cas_microstructure"] is ExecutionMode.PAPER
        assert len(warnings) == 1
        assert "measured limitation" in warnings[0]

    def test_g3_m1_cas_key_keeps_paper_on_slow_poll(self) -> None:
        cfg = _sample_config(
            poll_interval_seconds=60,
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                "M1_CAS": ExecutionMode.PAPER,
            },
        )
        validated_cfg, warnings = validate_startup_configuration(
            cfg, enforce_g3_shadow=False
        )
        assert validated_cfg.strategy_stances["M1_CAS"] is ExecutionMode.PAPER
        assert warnings

    def test_g3_enforce_flag_does_not_demote_cas(self) -> None:
        cfg = _sample_config(
            poll_interval_seconds=60,
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                "cas_microstructure": ExecutionMode.PAPER,
            },
            cas_event_driven=CasEventDrivenConfig(enabled=True),
        )
        validated_cfg, warnings = validate_startup_configuration(
            cfg, enforce_g3_shadow=True
        )
        assert validated_cfg.strategy_stances["cas_microstructure"] is ExecutionMode.PAPER
        assert validated_cfg.strategy_stances["positional_long_option"] is ExecutionMode.PAPER
        assert len(warnings) == 1
        assert "measured limitation" in warnings[0]

    def test_g3_fast_poll_does_not_warn_cas(self) -> None:
        cfg = _sample_config(
            poll_interval_seconds=10,
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                "cas_microstructure": ExecutionMode.PAPER,
            },
        )
        validated_cfg, warnings = validate_startup_configuration(
            cfg, enforce_g3_shadow=True
        )
        assert (
            validated_cfg.strategy_stances["cas_microstructure"] is ExecutionMode.PAPER
        )
        assert warnings == []

    def test_g1_min_lot_exceeds_budget_long_straddle(self) -> None:
        cfg = _sample_config(
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                "long_straddle": ExecutionMode.PAPER,
            },
        )
        with pytest.raises(
            StartupValidationError, match=r"Gate G1 violation.*long_straddle"
        ):
            validate_startup_configuration(cfg)

    def test_g1_min_lot_exceeds_budget_long_strangle(self) -> None:
        cfg = _sample_config(
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                "long_strangle": ExecutionMode.PAPER,
            },
        )
        with pytest.raises(
            StartupValidationError, match=r"Gate G1 violation.*long_strangle"
        ):
            validate_startup_configuration(cfg)

    def test_g1_budget_families_allowed_in_shadow(self) -> None:
        cfg = _sample_config(
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                "long_straddle": ExecutionMode.SHADOW,
                "long_strangle": ExecutionMode.SHADOW,
            },
        )
        validated_cfg, warnings = validate_startup_configuration(cfg)
        assert validated_cfg.strategy_stances["long_straddle"] is ExecutionMode.SHADOW
        assert warnings == []

    def test_g2_unproven_family_defined_risk_multileg(self) -> None:
        cfg = _sample_config(
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                "defined_risk_multileg": ExecutionMode.PAPER,
            },
        )
        with pytest.raises(
            StartupValidationError, match=r"Gate G2 violation.*defined_risk_multileg"
        ):
            validate_startup_configuration(cfg)

    def test_g2_unproven_family_iron_condor(self) -> None:
        cfg = _sample_config(
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                "iron_condor": ExecutionMode.PAPER,
            },
        )
        with pytest.raises(
            StartupValidationError, match=r"Gate G2 violation.*iron_condor"
        ):
            validate_startup_configuration(cfg)

    def test_g2_unproven_families_allowed_in_shadow(self) -> None:
        cfg = _sample_config(
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                "defined_risk_multileg": ExecutionMode.SHADOW,
                "iron_condor": ExecutionMode.SHADOW,
            },
        )
        validated_cfg, warnings = validate_startup_configuration(cfg)
        assert (
            validated_cfg.strategy_stances["defined_risk_multileg"]
            is ExecutionMode.SHADOW
        )
        assert warnings == []

    def test_commodity_underlying_paper_rejected(self) -> None:
        cfg = _sample_config(
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                "commodity_underlying": ExecutionMode.PAPER,
            },
        )
        with pytest.raises(
            StartupValidationError,
            match="Commodity futures execution is disabled on NIFTY paper deployment",
        ):
            validate_startup_configuration(cfg)

    def test_commodity_futures_trend_paper_rejected(self) -> None:
        cfg = _sample_config(
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                "commodity_futures_trend": ExecutionMode.PAPER,
            },
        )
        with pytest.raises(
            StartupValidationError,
            match="Commodity futures execution is disabled on NIFTY paper deployment",
        ):
            validate_startup_configuration(cfg)

    def test_unknown_family_rejected(self) -> None:
        cfg = _sample_config(
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                "unknown_experimental_strat": ExecutionMode.SHADOW,
            },
        )
        with pytest.raises(
            StartupValidationError,
            match="Unknown family, mode, or strategy in session stances",
        ):
            validate_startup_configuration(cfg)

    def test_shipped_paper_session_config_keeps_m1_paper(self) -> None:
        session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
        session_cfg = session_cfg.model_copy(
            update={
                "mode_stances": {
                    **session_cfg.mode_stances,
                    "M1_CAS": ExecutionMode.PAPER,
                }
            }
        )
        modes_cfg = load_modes_config(ROOT / "config" / "modes.yaml")
        validated_cfg, warnings = validate_startup_configuration(
            session_cfg, modes_cfg, enforce_g3_shadow=True
        )
        assert validated_cfg.mode_stances["M1_CAS"] is ExecutionMode.PAPER
        assert validated_cfg.mode_stances["M3_TACTICAL_POSITIONAL"] is ExecutionMode.PAPER
        assert validated_cfg.mode_stances["M4_STRATEGIC_POSITIONAL"] is ExecutionMode.PAPER
        assert len(warnings) == 1
        assert "measured limitation" in warnings[0]

    def test_shipped_paper_session_config_without_enforce_still_paper(self) -> None:
        session_cfg = load_paper_session_config(ROOT / "config" / "paper_session.yaml")
        session_cfg = session_cfg.model_copy(
            update={
                "mode_stances": {
                    **session_cfg.mode_stances,
                    "M1_CAS": ExecutionMode.PAPER,
                }
            }
        )
        modes_cfg = load_modes_config(ROOT / "config" / "modes.yaml")
        validated_cfg, warnings = validate_startup_configuration(
            session_cfg, modes_cfg, enforce_g3_shadow=False
        )
        assert validated_cfg.mode_stances["M1_CAS"] is ExecutionMode.PAPER
        assert warnings
