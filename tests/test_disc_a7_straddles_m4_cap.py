"""DISC-A7: straddle/strangle PAPER under DISCOVERY, M4 cap from config, G2 fix."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

import tests.factories as f
from tests.test_p11_m4_broad_basket import (
    IDENTIFICATION_POLICY,
    _market,
    _straddle_candidates,
)
from tests.test_p12_economic_overlap_m4_cap import NOW, _m4_condor
from trading.config.discovery import load_discovery_config
from trading.config.risk_policy import load_risk_policy
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.mode_policy import load_modes_config
from trading.domain.contracts.position import PositionState
from trading.domain.enums import (
    EntryProfile,
    ExecutionMode,
    FamilyId,
    ModeId,
    ReasonCode,
    TradeState,
)
from trading.domain.family_gates import (
    G1_EXCEEDS_BUDGET_FAMILIES,
    G2_UNPROVEN_FAMILIES,
    assert_family_gate_sets_use_valid_family_ids,
)
from trading.identification import bind_long_straddle
from trading.portfolio.arbitration import PortfolioArbiter
from trading.portfolio.economic_overlap import m4_open_position_cap
from trading.runtime.four_mode_producers import produce_family_requests
from trading.runtime.paper_session import PaperSessionConfig
from trading.runtime.startup_validation import (
    StartupValidationError,
    validate_startup_configuration,
)

ROOT = Path(__file__).resolve().parent.parent
MODES = load_modes_config(ROOT / "config" / "modes.yaml")
DISCOVERY = load_discovery_config(ROOT / "config" / "discovery.yaml").config
RISK = load_risk_policy(ROOT / "config" / "risk.yaml")


def _sample_session_config(**overrides: object) -> PaperSessionConfig:
    payload: dict[str, object] = {
        "poll_interval_seconds": 60,
        "eod_local": "15:40",
        "option_strikes_each_side": 2,
        "experiment_prefix": "EXP-TEST",
        "routing_profile": "four_mode",
        "mode_stances": {
            "M1_CAS": ExecutionMode.PAPER,
            "M2_DIRECTIONAL": ExecutionMode.PAPER,
            "M3_TACTICAL_POSITIONAL": ExecutionMode.PAPER,
            "M4_STRATEGIC_POSITIONAL": ExecutionMode.PAPER,
        },
        "family_stances": {
            "long_straddle": ExecutionMode.SHADOW,
            "long_strangle": ExecutionMode.SHADOW,
            "long_call_calendar": ExecutionMode.SUSPENDED,
            "long_put_calendar": ExecutionMode.SUSPENDED,
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


def _open_m4_position(trade_id: str) -> PositionState:
    return f.position_state(
        trade_id=trade_id,
        intent_id=f"{trade_id}-INT",
        mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
        state=TradeState.OPEN,
        exit_policy=f.exit_policy(trade_id=trade_id),
    )


class TestDiscA7FamilyGateSets:
    def test_g1_and_g2_sets_use_valid_family_ids(self) -> None:
        assert_family_gate_sets_use_valid_family_ids()
        for family in G1_EXCEEDS_BUDGET_FAMILIES | G2_UNPROVEN_FAMILIES:
            assert FamilyId(family)


class TestDiscA7StartupValidation:
    def test_discovery_straddle_paper_passes_startup(self) -> None:
        cfg = _sample_session_config(entry_profile=EntryProfile.DISCOVERY)
        validated, warnings = validate_startup_configuration(
            cfg,
            MODES,
            discovery_path=ROOT / "config" / "discovery.yaml",
        )
        assert validated.entry_profile is EntryProfile.DISCOVERY
        assert len(warnings) <= 1

    def test_strict_straddle_paper_still_refused(self) -> None:
        cfg = _sample_session_config(
            entry_profile=EntryProfile.STRICT,
            family_stances={
                "long_straddle": ExecutionMode.PAPER,
                "long_strangle": ExecutionMode.SHADOW,
                "long_call_calendar": ExecutionMode.SUSPENDED,
                "long_put_calendar": ExecutionMode.SUSPENDED,
            },
        )
        with pytest.raises(
            StartupValidationError, match=r"Gate G1 violation.*long_straddle"
        ):
            validate_startup_configuration(cfg, MODES)

    @pytest.mark.parametrize("profile", [EntryProfile.STRICT, EntryProfile.DISCOVERY])
    def test_calendar_paper_fails_in_both_profiles(self, profile: EntryProfile) -> None:
        cfg = _sample_session_config(
            entry_profile=profile,
            family_stances={
                "long_straddle": ExecutionMode.SHADOW,
                "long_strangle": ExecutionMode.SHADOW,
                "long_call_calendar": ExecutionMode.PAPER,
                "long_put_calendar": ExecutionMode.SUSPENDED,
            },
        )
        with pytest.raises(
            StartupValidationError,
            match=r"Calendar family 'long_call_calendar'",
        ):
            validate_startup_configuration(
                cfg,
                MODES,
                discovery_path=ROOT / "config" / "discovery.yaml",
            )


class TestDiscA7StraddleRouting:
    def test_discovery_bound_straddle_produces_executable_request(self) -> None:
        bound = bind_long_straddle(
            _straddle_candidates(),
            market=_market(),
            policy=IDENTIFICATION_POLICY,
        )
        assert bound.binding.eligible is True
        produced = produce_family_requests(
            modes_config=MODES,
            mode_stances={mode.value: ExecutionMode.PAPER for mode in ModeId},
            family_stances={
                "long_straddle": ExecutionMode.SHADOW,
                "long_strangle": ExecutionMode.SHADOW,
            },
            candidates=_straddle_candidates(),
            market=_market(),
            policy=IDENTIFICATION_POLICY,
            p1=None,
            master_symbols=frozenset(
                snap.contract.symbol for snap in _straddle_candidates()
            ),
            discovery_config=DISCOVERY,
            entry_profile=EntryProfile.DISCOVERY,
        )
        straddle_rows = [
            row for row in produced if row.spec.family_id is FamilyId.long_straddle
        ]
        assert len(straddle_rows) == 1
        row = straddle_rows[0]
        assert row.execute is True
        assert row.execution_mode is ExecutionMode.PAPER


class TestDiscA7M4CapFromConfig:
    def test_strict_cap_from_risk_yaml(self) -> None:
        assert m4_open_position_cap(risk_policy=RISK.config) == 2

    def test_discovery_cap_from_mode_config(self) -> None:
        assert m4_open_position_cap(discovery_config=DISCOVERY) == 4

    def test_discovery_fourth_m4_allowed_fifth_rejected(self) -> None:
        arbiter = PortfolioArbiter(max_m4_open_positions=4)
        existing = (
            _open_m4_position("TRD-1"),
            _open_m4_position("TRD-2"),
            _open_m4_position("TRD-3"),
        )
        fourth = _m4_condor("INTENT-M4-4", wing_shift=0)
        fifth = _m4_condor("INTENT-M4-5", wing_shift=50)
        result = arbiter.arbitrate(
            [
                cast(TradeIntent, fourth),
                cast(TradeIntent, fifth),
            ],
            existing_positions=existing,
            now=NOW,
        )
        assert len(result.approved_intents) == 1
        assert result.approved_intents[0].intent_id == "INTENT-M4-4"
        assert len(result.suppressed_intents) == 1
        assert (
            result.suppressed_intents[0].reason_code
            is ReasonCode.M4_POSITION_CAP_REACHED
        )
        assert result.suppressed_intents[0].candidate_intent_id == "INTENT-M4-5"
