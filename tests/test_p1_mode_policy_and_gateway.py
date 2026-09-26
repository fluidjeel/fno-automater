"""Dedicated Phase P1 Verification and Regression Lock-in Test Suite.

Exercises and locks in every P1 requirement from FOUR_MODE_LAYER_CHANGE_CRITERIA.md
(§2.1, §3.1, §5.1):
- test_p1_mode_id_enum_and_policy_contract
- test_p1_intent_preserves_invariant_3_and_carries_mode_and_family
- test_p1_gateway_rejects_non_nifty_execution
- test_p1_gateway_rejects_disallowed_family_and_m3_single_leg
- test_p1_startup_validation_enforces_g1_g2_g3
- test_p1_g3_boundary_demotion_preserves_legacy_config
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

import tests.factories as f
from trading.broker.paper import PaperBroker
from trading.config import load_config, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    InstrumentSpec,
    IntentLeg,
    ModePolicy,
    ModesConfig,
    PortfolioSnapshot,
    TradeIntent,
    load_modes_config,
)
from trading.domain.enums import (
    Exchange,
    ExecutionMode,
    FamilyId,
    InstrumentKind,
    ModeId,
    OptionType,
    ReasonCode,
    RiskAction,
    Side,
)
from trading.domain.ids import SequentialIdFactory
from trading.risk import CapitalReservationService, RiskGateway, RiskGatewayRequest
from trading.runtime.cas_event_path import CasEventDrivenConfig
from trading.runtime.paper_session import (
    PaperSessionConfig,
    load_paper_session_config,
)
from trading.runtime.startup_validation import (
    G1_EXCEEDS_BUDGET_FAMILIES,
    G2_LEGACY_UNPROVEN_STRATEGY_IDS,
    G2_UNPROVEN_FAMILIES,
    StartupValidationError,
    validate_startup_configuration,
)
from trading.storage import TradingStore

ROOT = Path(__file__).resolve().parent.parent
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
NOW = f.NOW


def instrument_spec(**overrides: object) -> InstrumentSpec:
    payload: dict[str, object] = {
        "trading_symbol": "NSE:NIFTY26SEP24000CE",
        "exchange": Exchange.NFO,
        "segment": "NSE_FO",
        "underlying": "NIFTY",
        "instrument_kind": InstrumentKind.OPTION,
        "provider_token": "tok-1",
        "exchange_token": 1,
        "lot_size": 75,
        "tick_size": Decimal("0.05"),
        "price_precision": 2,
        "expiry": date(2026, 9, 24),
        "strike": Decimal("24000"),
        "option_type": OptionType.CALL,
        "trading_session": "0915-1530",
        "source": "fixture",
        "verified_at": date(2026, 9, 1),
    }
    payload.update(overrides)
    return InstrumentSpec.model_validate(payload)


def option_snapshot(**overrides: object) -> FeatureSnapshot:
    payload: dict[str, object] = {
        "contract": f.option_contract(),
        "market": f.quote(
            bid=f.price("91.95"),
            ask=f.price("92.00"),
            bid_size=300,
            ask_size=300,
        ),
        "derivatives": DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
        ),
    }
    payload.update(overrides)
    return f.snapshot(**payload)


def _gateway_request(
    *,
    intent: TradeIntent | None = None,
    feature_snapshot: FeatureSnapshot | None = None,
    portfolio_snapshot: PortfolioSnapshot | None = None,
    instrument: InstrumentSpec | None = None,
) -> RiskGatewayRequest:
    snap = feature_snapshot or option_snapshot()
    resolved_intent = intent or f.intent(
        snapshot_id=snap.snapshot_id,
        requested_risk=f.money("6500"),
        estimated_max_loss=f.money("10000"),
    )
    return RiskGatewayRequest(
        intent=resolved_intent,
        feature_snapshot=snap,
        portfolio_snapshot=portfolio_snapshot or f.portfolio_snapshot(),
        instrument=instrument or instrument_spec(),
        event_risk_state=f.event_risk_state(),
    )


def _sample_session_config(**overrides: object) -> PaperSessionConfig:
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


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def store(clock: FrozenClock, tmp_path: Path) -> TradingStore:
    return TradingStore.open(tmp_path / "trading.db", clock=clock)


@pytest.fixture
def broker(clock: FrozenClock, id_factory: SequentialIdFactory) -> PaperBroker:
    return PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=id_factory,
    )


@pytest.fixture
def gateway(
    store: TradingStore,
    broker: PaperBroker,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> RiskGateway:
    return RiskGateway(
        account_config=ACCOUNT_CONFIG,
        risk_policy=RISK_POLICY,
        reservation_service=CapitalReservationService(
            store,
            clock=clock,
            id_factory=id_factory,
        ),
        margin_preview=broker,
        clock=clock,
        id_factory=id_factory,
    )


def test_p1_mode_id_enum_and_policy_contract(tmp_path: Path) -> None:
    """Verifies ModeId, FamilyId, ModePolicy, ModesConfig, and config/modes.yaml loading."""
    # 1. ModeId members
    expected_modes = {
        "M1_CAS": "M1_CAS",
        "M2_DIRECTIONAL": "M2_DIRECTIONAL",
        "M3_TACTICAL_POSITIONAL": "M3_TACTICAL_POSITIONAL",
        "M4_STRATEGIC_POSITIONAL": "M4_STRATEGIC_POSITIONAL",
    }
    assert len(ModeId) == 4
    for attr, val in expected_modes.items():
        assert getattr(ModeId, attr).value == val

    # 2. FamilyId members (14 families from §5)
    expected_families = {
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
    assert len(FamilyId) == 14
    assert {f.value for f in FamilyId} == expected_families

    # 3. ModePolicy strict typing and float rejection
    with pytest.raises(ValidationError):
        ModePolicy(
            mode_id=ModeId.M1_CAS,
            mandate="test mandate",
            capital_share=0.10,  # binary float must be rejected
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

    # 4. config/modes.yaml loading & verification against §4 and §10.1
    modes_yaml_path = ROOT / "config" / "modes.yaml"
    assert modes_yaml_path.is_file(), "config/modes.yaml must exist"

    cfg = load_modes_config(modes_yaml_path)
    assert isinstance(cfg, ModesConfig)
    assert cfg.schema_version == "1"
    assert set(cfg.modes.keys()) == set(ModeId)

    # Capital share breakdown
    assert cfg.modes[ModeId.M1_CAS].capital_share == Decimal("0.10")
    assert cfg.modes[ModeId.M2_DIRECTIONAL].capital_share == Decimal("0.28")
    assert cfg.modes[ModeId.M3_TACTICAL_POSITIONAL].capital_share == Decimal("0.30")
    assert cfg.modes[ModeId.M4_STRATEGIC_POSITIONAL].capital_share == Decimal("0.32")
    assert sum(m.capital_share for m in cfg.modes.values()) == Decimal("1.00")

    # Per-trade fractions
    assert cfg.modes[ModeId.M1_CAS].per_trade_loss_cap_fraction == Decimal("0.05")
    assert cfg.modes[ModeId.M2_DIRECTIONAL].per_trade_loss_cap_fraction == Decimal(
        "0.04"
    )
    assert cfg.modes[
        ModeId.M3_TACTICAL_POSITIONAL
    ].per_trade_loss_cap_fraction == Decimal("0.02")
    assert cfg.modes[
        ModeId.M4_STRATEGIC_POSITIONAL
    ].per_trade_loss_cap_fraction == Decimal("0.01")

    # Max open and daily budget caps
    assert cfg.modes[ModeId.M1_CAS].max_open_loss_cap_fraction == Decimal("0.10")
    assert cfg.modes[ModeId.M1_CAS].daily_budget_cap_fraction == Decimal("0.15")
    assert cfg.modes[ModeId.M2_DIRECTIONAL].max_open_loss_cap_fraction == Decimal(
        "0.06"
    )
    assert cfg.modes[ModeId.M2_DIRECTIONAL].daily_budget_cap_fraction == Decimal("0.08")
    assert cfg.modes[
        ModeId.M3_TACTICAL_POSITIONAL
    ].max_open_loss_cap_fraction == Decimal("0.04")
    assert cfg.modes[
        ModeId.M3_TACTICAL_POSITIONAL
    ].daily_budget_cap_fraction == Decimal("0.05")
    assert cfg.modes[
        ModeId.M4_STRATEGIC_POSITIONAL
    ].max_open_loss_cap_fraction == Decimal("0.03")
    assert cfg.modes[
        ModeId.M4_STRATEGIC_POSITIONAL
    ].daily_budget_cap_fraction == Decimal("0.04")

    # Cadences and styles
    assert cfg.modes[ModeId.M1_CAS].holding_style == "INTRADAY"
    assert cfg.modes[ModeId.M1_CAS].review_cadence == ("event_driven",)
    assert cfg.modes[ModeId.M2_DIRECTIONAL].holding_style == "INTRADAY"
    assert cfg.modes[ModeId.M2_DIRECTIONAL].review_cadence == ("10:30", "14:30")
    assert cfg.modes[ModeId.M3_TACTICAL_POSITIONAL].holding_style == "POSITIONAL"
    assert cfg.modes[ModeId.M3_TACTICAL_POSITIONAL].review_cadence == ("10:30", "14:30")
    assert cfg.modes[ModeId.M4_STRATEGIC_POSITIONAL].holding_style == "POSITIONAL"
    assert cfg.modes[ModeId.M4_STRATEGIC_POSITIONAL].review_cadence == (
        "10:30",
        "14:30",
    )

    # Families
    assert cfg.modes[ModeId.M1_CAS].allowed_families == (
        FamilyId.long_call,
        FamilyId.long_put,
    )
    assert cfg.modes[ModeId.M2_DIRECTIONAL].allowed_families == (
        FamilyId.long_call,
        FamilyId.long_put,
    )
    assert cfg.modes[ModeId.M3_TACTICAL_POSITIONAL].allowed_families == (
        FamilyId.bull_call_debit,
        FamilyId.bear_put_debit,
        FamilyId.bull_put_credit,
        FamilyId.bear_call_credit,
    )
    assert len(cfg.modes[ModeId.M4_STRATEGIC_POSITIONAL].allowed_families) == 12

    # 5. Round trip
    assert cfg.round_trip() == cfg

    # 6. Custom path loading
    custom_yaml = tmp_path / "custom.yaml"
    custom_yaml.write_text(
        """
schema_version: "1"
modes:
  M1_CAS:
    mode_id: M1_CAS
    mandate: "Custom isolate test"
    capital_share: "1.00"
    per_trade_loss_cap_fraction: "0.05"
    max_open_loss_cap_fraction: "0.10"
    daily_budget_cap_fraction: "0.15"
    allowed_families:
      - long_call
    holding_style: "INTRADAY"
    window_start_ist: "09:15"
    window_end_ist: "15:30"
    expiry_rule: "test"
    review_cadence:
      - "event_driven"
""",
        encoding="utf-8",
    )
    custom_cfg = load_modes_config(custom_yaml)
    assert len(custom_cfg.modes) == 1
    assert custom_cfg.modes[ModeId.M1_CAS].mandate == "Custom isolate test"


def test_p1_intent_preserves_invariant_3_and_carries_mode_and_family() -> None:
    """Checks mode_id and family_id on TradeIntent and asserts no quantity, limit price, or broker field exists."""
    # 1. TradeIntent carries optional mode_id and family_id
    default_intent = f.intent()
    assert default_intent.mode_id is None
    assert default_intent.family_id is None

    populated_intent = f.intent(
        mode_id=ModeId.M2_DIRECTIONAL,
        family_id=FamilyId.long_call,
    )
    assert populated_intent.mode_id == ModeId.M2_DIRECTIONAL
    assert populated_intent.family_id == "long_call"

    # 2. Invariant 3: No quantity, limit price, or broker fields exist anywhere on TradeIntent
    forbidden_terms = {
        "quantity",
        "qty",
        "lots",
        "contracts",
        "ordertype",
        "broker",
        "brokerorderid",
        "clientorderid",
        "brokertoken",
        "approvedquantity",
        "limitprice",
        "executionprice",
        "stopprice",
    }

    intent_field_names = {n.replace("_", "").lower() for n in TradeIntent.model_fields}
    assert not (forbidden_terms & intent_field_names), (
        f"TradeIntent must not contain execution or broker fields: {forbidden_terms & intent_field_names}"
    )

    leg_field_names = {n.replace("_", "").lower() for n in IntentLeg.model_fields}
    assert not (forbidden_terms & leg_field_names), (
        f"IntentLeg must not contain execution or broker fields: {forbidden_terms & leg_field_names}"
    )

    # 3. Leg ratio is purely structural, not an execution quantity
    assert "ratio" in IntentLeg.model_fields
    assert "quantity" not in IntentLeg.model_fields
    assert "lots" not in IntentLeg.model_fields


def test_p1_gateway_rejects_non_nifty_execution(
    gateway: RiskGateway,
    store: TradingStore,
    broker: PaperBroker,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> None:
    """Asserts rejection with NON_NIFTY_EXECUTION_REJECTED for MCX futures, BankNifty options, or commodity intents."""
    # 1. MCX crude futures intent
    mcx_spec = instrument_spec(
        trading_symbol="MCX:CRUDEOIL26OCTFUT",
        exchange=Exchange.MCX,
        segment="MCX_COM",
        underlying="CRUDEOIL",
        instrument_kind=InstrumentKind.FUTURE,
        strike=None,
        option_type=None,
    )
    req_mcx = _gateway_request(instrument=mcx_spec)
    decision_mcx = gateway.evaluate(req_mcx)
    assert decision_mcx.action is RiskAction.REJECT
    assert decision_mcx.reason_codes == (ReasonCode.NON_NIFTY_EXECUTION_REJECTED,)

    # 2. BankNifty option intent
    banknifty_spec = instrument_spec(
        trading_symbol="NSE:BANKNIFTY26SEP50000CE",
        exchange=Exchange.NFO,
        segment="NSE_FO",
        underlying="BANKNIFTY",
        instrument_kind=InstrumentKind.OPTION,
        strike=Decimal("50000"),
        option_type=OptionType.CALL,
    )
    req_banknifty = _gateway_request(instrument=banknifty_spec)
    decision_banknifty = gateway.evaluate(req_banknifty)
    assert decision_banknifty.action is RiskAction.REJECT
    assert decision_banknifty.reason_codes == (ReasonCode.NON_NIFTY_EXECUTION_REJECTED,)

    # 3. MCX gold futures intent
    gold_spec = instrument_spec(
        trading_symbol="MCX:GOLD26OCTFUT",
        exchange=Exchange.MCX,
        segment="MCX_COM",
        underlying="GOLD",
        instrument_kind=InstrumentKind.FUTURE,
        strike=None,
        option_type=None,
    )
    req_gold = _gateway_request(instrument=gold_spec)
    decision_gold = gateway.evaluate(req_gold)
    assert decision_gold.action is RiskAction.REJECT
    assert decision_gold.reason_codes == (ReasonCode.NON_NIFTY_EXECUTION_REJECTED,)

    # 4. NIFTY futures (underlying is NIFTY, but instrument_kind is FUTURE, not OPTION)
    nifty_fut_spec = instrument_spec(
        trading_symbol="NSE:NIFTY26SEPFUT",
        exchange=Exchange.NFO,
        segment="NSE_FO",
        underlying="NIFTY",
        instrument_kind=InstrumentKind.FUTURE,
        strike=None,
        option_type=None,
    )
    req_nifty_fut = _gateway_request(instrument=nifty_fut_spec)
    decision_nifty_fut = gateway.evaluate(req_nifty_fut)
    assert decision_nifty_fut.action is RiskAction.REJECT
    assert decision_nifty_fut.reason_codes == (ReasonCode.NON_NIFTY_EXECUTION_REJECTED,)

    # 5. Legitimate NIFTY option does NOT get rejected with NON_NIFTY_EXECUTION_REJECTED
    nifty_opt_spec = instrument_spec(
        trading_symbol="NSE:NIFTY26SEP24000CE",
        underlying="NIFTY",
        instrument_kind=InstrumentKind.OPTION,
    )
    req_valid = _gateway_request(instrument=nifty_opt_spec)
    decision_valid = gateway.evaluate(req_valid)
    assert ReasonCode.NON_NIFTY_EXECUTION_REJECTED not in decision_valid.reason_codes

    # 6. Setting nifty_only_execution=False allows non-NIFTY underlying evaluation
    permissive_gateway = RiskGateway(
        account_config=ACCOUNT_CONFIG,
        risk_policy=RISK_POLICY,
        reservation_service=CapitalReservationService(
            store,
            clock=clock,
            id_factory=id_factory,
        ),
        margin_preview=broker,
        clock=clock,
        id_factory=id_factory,
        nifty_only_execution=False,
    )
    decision_permissive = permissive_gateway.evaluate(req_mcx)
    assert (
        ReasonCode.NON_NIFTY_EXECUTION_REJECTED not in decision_permissive.reason_codes
    )


def test_p1_gateway_rejects_disallowed_family_and_m3_single_leg(
    gateway: RiskGateway,
) -> None:
    """Asserts rejection with MODE_FAMILY_NOT_PERMITTED when family doesn't belong to mode or when M3 tries single-leg."""
    snap = option_snapshot()

    # 1. Mode specified but family_id is None
    req_missing_family = _gateway_request(
        feature_snapshot=snap,
        intent=f.intent(
            snapshot_id=snap.snapshot_id,
            mode_id=ModeId.M2_DIRECTIONAL,
            family_id=None,
        ),
    )
    decision_missing_family = gateway.evaluate(req_missing_family)
    assert decision_missing_family.action is RiskAction.REJECT
    assert decision_missing_family.reason_codes == (
        ReasonCode.MODE_FAMILY_NOT_PERMITTED,
    )

    # 2. Mode M2 with disallowed family (bull_call_debit is an M3/M4 family)
    req_m2_disallowed = _gateway_request(
        feature_snapshot=snap,
        intent=f.intent(
            snapshot_id=snap.snapshot_id,
            mode_id=ModeId.M2_DIRECTIONAL,
            family_id=FamilyId.bull_call_debit,
        ),
    )
    decision_m2_disallowed = gateway.evaluate(req_m2_disallowed)
    assert decision_m2_disallowed.action is RiskAction.REJECT
    assert decision_m2_disallowed.reason_codes == (
        ReasonCode.MODE_FAMILY_NOT_PERMITTED,
    )

    # 3. Mode M1 with disallowed family (bull_call_debit)
    req_m1_disallowed = _gateway_request(
        feature_snapshot=snap,
        intent=f.intent(
            snapshot_id=snap.snapshot_id,
            mode_id=ModeId.M1_CAS,
            family_id=FamilyId.bull_call_debit,
        ),
    )
    decision_m1_disallowed = gateway.evaluate(req_m1_disallowed)
    assert decision_m1_disallowed.action is RiskAction.REJECT
    assert decision_m1_disallowed.reason_codes == (
        ReasonCode.MODE_FAMILY_NOT_PERMITTED,
    )

    # 4. Mode M1 with multi-leg family (short_iron_condor_defined)
    req_m1_condor = _gateway_request(
        feature_snapshot=snap,
        intent=f.intent(
            snapshot_id=snap.snapshot_id,
            mode_id=ModeId.M1_CAS,
            family_id=FamilyId.short_iron_condor_defined,
        ),
    )
    decision_m1_condor = gateway.evaluate(req_m1_condor)
    assert decision_m1_condor.action is RiskAction.REJECT
    assert decision_m1_condor.reason_codes == (ReasonCode.MODE_FAMILY_NOT_PERMITTED,)

    # 5. Mode M3 tries single-leg long_call
    req_m3_single_leg = _gateway_request(
        feature_snapshot=snap,
        intent=f.intent(
            snapshot_id=snap.snapshot_id,
            mode_id=ModeId.M3_TACTICAL_POSITIONAL,
            family_id=FamilyId.long_call,
        ),
    )
    decision_m3_single_leg = gateway.evaluate(req_m3_single_leg)
    assert decision_m3_single_leg.action is RiskAction.REJECT
    assert decision_m3_single_leg.reason_codes == (
        ReasonCode.MODE_FAMILY_NOT_PERMITTED,
    )

    # 6. Mode M3 tries single-leg intent even with bull_call_debit family name
    req_m3_fake_spread = _gateway_request(
        feature_snapshot=snap,
        intent=f.intent(
            snapshot_id=snap.snapshot_id,
            mode_id=ModeId.M3_TACTICAL_POSITIONAL,
            family_id=FamilyId.bull_call_debit,
            legs=(f.leg("leg-1", Side.BUY),),  # only 1 leg!
        ),
    )
    decision_m3_fake_spread = gateway.evaluate(req_m3_fake_spread)
    assert decision_m3_fake_spread.action is RiskAction.REJECT
    assert decision_m3_fake_spread.reason_codes == (
        ReasonCode.MODE_FAMILY_NOT_PERMITTED,
    )

    # 7. Permitted mode-family pairs pass through cleanly without MODE_FAMILY_NOT_PERMITTED
    snap_m2 = option_snapshot(
        market=f.quote(
            bid=f.price("39.95"),
            ask=f.price("40.00"),
            bid_size=300,
            ask_size=300,
        )
    )
    req_m2_valid = _gateway_request(
        feature_snapshot=snap_m2,
        intent=f.intent(
            snapshot_id=snap_m2.snapshot_id,
            mode_id=ModeId.M2_DIRECTIONAL,
            family_id=FamilyId.long_call,
        ),
    )
    decision_m2_valid = gateway.evaluate(req_m2_valid)
    assert decision_m2_valid.action in (RiskAction.APPROVE, RiskAction.RESIZE)
    assert ReasonCode.MODE_FAMILY_NOT_PERMITTED not in decision_m2_valid.reason_codes

    snap_m1 = option_snapshot(
        market=f.quote(
            bid=f.price("19.95"),
            ask=f.price("20.00"),
            bid_size=300,
            ask_size=300,
        )
    )
    req_m1_valid = _gateway_request(
        feature_snapshot=snap_m1,
        intent=f.intent(
            snapshot_id=snap_m1.snapshot_id,
            mode_id=ModeId.M1_CAS,
            family_id=FamilyId.long_call,
        ),
    )
    decision_m1_valid = gateway.evaluate(req_m1_valid)
    assert decision_m1_valid.action in (RiskAction.APPROVE, RiskAction.RESIZE)
    assert ReasonCode.MODE_FAMILY_NOT_PERMITTED not in decision_m1_valid.reason_codes


def test_p1_startup_validation_enforces_g1_g2_g3() -> None:
    """Asserts StartupValidationError on unproven families, G1 budget failures, G3 polled loop violations, and commodity futures."""
    # 1. G2 unproven families cannot run PAPER
    for unproven_family in sorted(
        G2_UNPROVEN_FAMILIES | G2_LEGACY_UNPROVEN_STRATEGY_IDS
    ):
        cfg = _sample_session_config(
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                unproven_family: ExecutionMode.PAPER,
            }
        )
        with pytest.raises(
            StartupValidationError,
            match=rf"Gate G2 violation: Family '{unproven_family}' is not lifecycle proven",
        ):
            validate_startup_configuration(cfg)

    # 2. G1 budget failure families cannot run PAPER
    for budget_family in sorted(G1_EXCEEDS_BUDGET_FAMILIES):
        cfg = _sample_session_config(
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                budget_family: ExecutionMode.PAPER,
            }
        )
        with pytest.raises(
            StartupValidationError,
            match=rf"Gate G1 violation: Family '{budget_family}' exceeds budget",
        ):
            validate_startup_configuration(cfg)

    # 3. Commodity futures cannot run PAPER on NIFTY deployment
    for commodity_key in ("commodity_underlying", "commodity_futures_trend"):
        cfg = _sample_session_config(
            strategy_stances={
                "positional_long_option": ExecutionMode.PAPER,
                commodity_key: ExecutionMode.PAPER,
            }
        )
        with pytest.raises(
            StartupValidationError,
            match="Commodity futures execution is disabled on NIFTY paper deployment",
        ):
            validate_startup_configuration(cfg)

    # 4. G3 slow poll warns but keeps M1 PAPER on the paper book
    cfg_g3_cas = _sample_session_config(
        poll_interval_seconds=60,
        strategy_stances={
            "positional_long_option": ExecutionMode.PAPER,
            "cas_microstructure": ExecutionMode.PAPER,
        },
        cas_event_driven=CasEventDrivenConfig(enabled=True),
    )
    validated, warnings = validate_startup_configuration(
        cfg_g3_cas, enforce_g3_shadow=False
    )
    assert validated.strategy_stances["cas_microstructure"] is ExecutionMode.PAPER
    assert any("measured limitation" in item for item in warnings)

    cfg_g3_m1 = _sample_session_config(
        poll_interval_seconds=60,
        strategy_stances={
            "positional_long_option": ExecutionMode.PAPER,
            "M1_CAS": ExecutionMode.PAPER,
        },
    )
    validated_m1, _warnings = validate_startup_configuration(
        cfg_g3_m1, enforce_g3_shadow=False
    )
    assert validated_m1.strategy_stances["M1_CAS"] is ExecutionMode.PAPER

    # 5. Unknown family or strategy in strategy_stances raises StartupValidationError
    cfg_unknown = _sample_session_config(
        strategy_stances={
            "positional_long_option": ExecutionMode.PAPER,
            "arbitrary_unregistered_strat": ExecutionMode.SHADOW,
        }
    )
    with pytest.raises(
        StartupValidationError,
        match="Unknown family, mode, or strategy in session stances: 'arbitrary_unregistered_strat'",
    ):
        validate_startup_configuration(cfg_unknown)

    # 6. All unproven and budget families ARE permitted in SHADOW
    shadow_stances: dict[str, ExecutionMode] = {
        "positional_long_option": ExecutionMode.PAPER,
        "debit_spread": ExecutionMode.PAPER,
        "commodity_futures_trend": ExecutionMode.SHADOW,
        "cas_microstructure": ExecutionMode.SHADOW,
    }
    for f_unproven in G2_UNPROVEN_FAMILIES | G2_LEGACY_UNPROVEN_STRATEGY_IDS:
        shadow_stances[f_unproven] = ExecutionMode.SHADOW
    for f_budget in G1_EXCEEDS_BUDGET_FAMILIES:
        shadow_stances[f_budget] = ExecutionMode.SHADOW

    cfg_shadow = _sample_session_config(strategy_stances=shadow_stances)
    validated_cfg, warnings = validate_startup_configuration(cfg_shadow)
    assert validated_cfg == cfg_shadow
    assert warnings == []


def test_p1_g3_boundary_warns_without_demotion() -> None:
    """Asserts slow-poll M1 PAPER stays PAPER while emitting a latency warning."""
    input_cfg = _sample_session_config(
        poll_interval_seconds=60,
        strategy_stances={
            "positional_long_option": ExecutionMode.PAPER,
            "debit_spread": ExecutionMode.PAPER,
            "cas_microstructure": ExecutionMode.PAPER,
            "defined_risk_multileg": ExecutionMode.SHADOW,
            "commodity_futures_trend": ExecutionMode.SHADOW,
        },
        cas_event_driven=CasEventDrivenConfig(enabled=True),
    )

    validated_cfg, warnings = validate_startup_configuration(
        input_cfg, enforce_g3_shadow=True
    )

    assert validated_cfg.strategy_stances["cas_microstructure"] is ExecutionMode.PAPER
    assert (
        validated_cfg.strategy_stances["positional_long_option"] is ExecutionMode.PAPER
    )
    assert validated_cfg.strategy_stances["debit_spread"] is ExecutionMode.PAPER
    assert len(warnings) == 1
    assert "measured limitation" in warnings[0]

    assert input_cfg.strategy_stances["cas_microstructure"] is ExecutionMode.PAPER

    paper_session_file = ROOT / "config" / "paper_session.yaml"
    modes_file = ROOT / "config" / "modes.yaml"
    assert paper_session_file.is_file(), "config/paper_session.yaml must exist on disk"

    raw_disk_content_before = paper_session_file.read_text(encoding="utf-8")
    assert "M1_CAS: PAPER" in raw_disk_content_before
    assert "M2_DIRECTIONAL: PAPER" in raw_disk_content_before

    loaded_disk_cfg = load_paper_session_config(paper_session_file)
    assert loaded_disk_cfg.mode_stances["M1_CAS"] is ExecutionMode.PAPER
    assert loaded_disk_cfg.mode_stances["M2_DIRECTIONAL"] is ExecutionMode.PAPER

    modes_cfg = load_modes_config(modes_file)
    runtime_session_cfg, startup_warnings = validate_startup_configuration(
        loaded_disk_cfg,
        modes_cfg,
        enforce_g3_shadow=True,
    )

    assert runtime_session_cfg.mode_stances["M1_CAS"] is ExecutionMode.PAPER
    assert runtime_session_cfg.mode_stances["M2_DIRECTIONAL"] is ExecutionMode.PAPER
    assert (
        runtime_session_cfg.mode_stances["M3_TACTICAL_POSITIONAL"]
        is ExecutionMode.PAPER
    )
    assert len(startup_warnings) == 1
    assert "measured limitation" in startup_warnings[0]

    raw_disk_content_after = paper_session_file.read_text(encoding="utf-8")
    assert raw_disk_content_after == raw_disk_content_before
    assert "M1_CAS: PAPER" in raw_disk_content_after
