from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

import tests.factories as f
from trading.domain.contracts import (
    ModePolicy,
    ModesConfig,
    TradeIntent,
    load_modes_config,
)
from trading.domain.enums import FamilyId, ModeId, ReasonCode


def test_family_id_members() -> None:
    """All 14 strategy families from §5 are defined in FamilyId."""
    expected = {
        "long_call",
        "long_put",
        "bull_call_debit",
        "bear_put_debit",
        "bull_put_credit",
        "bear_call_credit",
        "short_iron_condor_defined",
        "short_iron_butterfly_defined",
        "long_call_butterfly",
        "long_put_butterfly",
        "long_straddle",
        "long_strangle",
        "long_call_calendar",
        "long_put_calendar",
    }
    assert {m.value for m in FamilyId} == expected
    for name in expected:
        assert getattr(FamilyId, name).value == name


def test_reason_code_new_members() -> None:
    """New reason codes are present in ReasonCode."""
    assert ReasonCode.NON_NIFTY_EXECUTION_REJECTED == "NON_NIFTY_EXECUTION_REJECTED"
    assert ReasonCode.MODE_FAMILY_NOT_PERMITTED == "MODE_FAMILY_NOT_PERMITTED"


def test_trade_intent_mode_and_family_fields() -> None:
    """TradeIntent accepts optional mode_id and family_id."""
    intent_default = f.intent()
    assert intent_default.mode_id is None
    assert intent_default.family_id is None

    intent_with_mode = f.intent(
        mode_id=ModeId.M2_DIRECTIONAL,
        family_id=FamilyId.long_call,
    )
    assert intent_with_mode.mode_id == ModeId.M2_DIRECTIONAL
    assert intent_with_mode.family_id == "long_call"


def test_trade_intent_invariant_03_preserved() -> None:
    """Invariant 3 remains intact: no broker fields or quantities on intent."""
    names = {n.replace("_", "") for n in TradeIntent.model_fields}
    forbidden = {
        "quantity",
        "lots",
        "contracts",
        "ordertype",
        "brokerorderid",
        "clientorderid",
        "brokertoken",
        "approvedquantity",
    }
    assert not (forbidden & names)


def test_load_modes_config() -> None:
    """config/modes.yaml loads, validates, and matches §4 and §10.1."""
    config = load_modes_config()
    assert config.schema_version == "1"
    assert len(config.modes) == 4

    m1 = config.modes[ModeId.M1_CAS]
    assert m1.mode_id == ModeId.M1_CAS
    assert m1.capital_share == Decimal("0.10")
    assert m1.per_trade_loss_cap_fraction == Decimal("0.05")
    assert m1.max_open_loss_cap_fraction == Decimal("0.10")
    assert m1.daily_budget_cap_fraction == Decimal("0.15")
    assert m1.allowed_families == (FamilyId.long_call, FamilyId.long_put)
    assert m1.holding_style == "INTRADAY"

    m2 = config.modes[ModeId.M2_DIRECTIONAL]
    assert m2.mode_id == ModeId.M2_DIRECTIONAL
    assert m2.capital_share == Decimal("0.20")
    assert m2.per_trade_loss_cap_fraction == Decimal("0.03")
    assert m2.max_open_loss_cap_fraction == Decimal("0.06")
    assert m2.daily_budget_cap_fraction == Decimal("0.08")
    assert m2.allowed_families == (FamilyId.long_call, FamilyId.long_put)
    assert m2.holding_style == "INTRADAY"

    m3 = config.modes[ModeId.M3_TACTICAL_POSITIONAL]
    assert m3.mode_id == ModeId.M3_TACTICAL_POSITIONAL
    assert m3.capital_share == Decimal("0.30")
    assert m3.per_trade_loss_cap_fraction == Decimal("0.02")
    assert m3.max_open_loss_cap_fraction == Decimal("0.04")
    assert m3.daily_budget_cap_fraction == Decimal("0.05")
    assert m3.allowed_families == (
        FamilyId.bull_call_debit,
        FamilyId.bear_put_debit,
        FamilyId.bull_put_credit,
        FamilyId.bear_call_credit,
    )
    assert m3.holding_style == "POSITIONAL"

    m4 = config.modes[ModeId.M4_STRATEGIC_POSITIONAL]
    assert m4.mode_id == ModeId.M4_STRATEGIC_POSITIONAL
    assert m4.capital_share == Decimal("0.40")
    assert m4.per_trade_loss_cap_fraction == Decimal("0.01")
    assert m4.max_open_loss_cap_fraction == Decimal("0.03")
    assert m4.daily_budget_cap_fraction == Decimal("0.04")
    assert len(m4.allowed_families) == 12
    assert m4.holding_style == "POSITIONAL"

    # Total capital share sums to 1.00
    total_share = sum(p.capital_share for p in config.modes.values())
    assert total_share == Decimal("1.00")


def test_modes_config_round_trip() -> None:
    """ModesConfig round-trips losslessly through JSON primitives."""
    config: ModesConfig = load_modes_config()
    round_tripped = config.round_trip()
    assert round_tripped == config


def test_mode_policy_rejects_floats() -> None:
    """ModePolicy rejects binary floats in numeric fields."""
    with pytest.raises(ValidationError):
        ModePolicy(
            mode_id=ModeId.M1_CAS,
            mandate="test",
            capital_share=0.10,  # type: ignore[arg-type]
            per_trade_loss_cap_fraction=Decimal("0.05"),
            max_open_loss_cap_fraction=Decimal("0.10"),
            daily_budget_cap_fraction=Decimal("0.15"),
            allowed_families=(FamilyId.long_call,),
            holding_style="INTRADAY",
            window_start_ist="09:15",
            window_end_ist="15:30",
            expiry_rule="test",
            review_cadence=("event_driven",),
        )


def test_load_modes_config_custom_path(tmp_path: Path) -> None:
    """load_modes_config respects explicit path parameter."""
    custom_yaml = tmp_path / "custom_modes.yaml"
    custom_yaml.write_text(
        """
schema_version: "1"
modes:
  M1_CAS:
    mode_id: M1_CAS
    mandate: "Custom mandate"
    capital_share: "1.00"
    per_trade_loss_cap_fraction: "0.05"
    max_open_loss_cap_fraction: "0.10"
    daily_budget_cap_fraction: "0.15"
    allowed_families:
      - long_call
    holding_style: "INTRADAY"
    window_start_ist: "09:15"
    window_end_ist: "15:30"
    expiry_rule: "custom"
    review_cadence:
      - "event_driven"
""",
        encoding="utf-8",
    )
    cfg = load_modes_config(custom_yaml)
    assert len(cfg.modes) == 1
    assert cfg.modes[ModeId.M1_CAS].mandate == "Custom mandate"
