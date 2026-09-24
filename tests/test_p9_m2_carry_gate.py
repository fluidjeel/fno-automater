"""Phase P9 M2 Carry Gate Test Suite.

Locks in Phase P9 requirements from:
- FOUR_MODE_LAYER_CHANGE_CRITERIA.md (§2.15)
- NIFTY_FOUR_MODE_CURSOR_REDESIGN.md (§4.2, Scenarios T18/T19, Example F)
- FOUR_MODE_REDESIGN_PLAN.md (Phase P9)
- REQUIREMENT_TRACEABILITY.md (T18, T19)

Verifications:
1. Carry records ``CARRY_APPROVED`` only when all required evidence passes.
2. Carry records ``CARRY_REJECTED`` when any required evidence is missing.
3. A profitable mark alone does not approve carry (T18/T19).
4. A losing trade does not receive a looser standard; it may still carry when
   evidence passes (T19).
5. Rejected carry sets ``exit_initiated`` and the position mode stays M2.
6. Legacy ``evaluate_carry_forward`` profit requirement is not used on this path.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import tests.factories as f
from trading.domain.contracts.carry import M2CarryGateConfig, M2CarryGateInput
from trading.domain.contracts.identification import (
    MacroStatus,
    MarketState,
    SetupFeatures,
    StructureKind,
    TrendState,
    VolatilityState,
)
from trading.domain.contracts.intent import IntentLeg, TradeIntent
from trading.domain.contracts.position import PositionState
from trading.domain.enums import (
    CarryGateAction,
    DataQuality,
    FamilyId,
    ModeId,
    ReasonCode,
    Side,
)
from trading.domain.primitives import Currency, Money
from trading.trade.carry_gate import (
    assess_m2_thesis,
    build_m2_carry_gate_input,
    evaluate_m2_carry_gate,
    load_carry_gate_config,
)
from trading.trade.eod_scanner import evaluate_carry_forward
from trading.universe.contracts import (
    CarryForwardAction,
    ConvictionAssessment,
    ConvictionLevel,
    TradeDirection,
)

NOW = datetime(2026, 9, 24, 14, 55, tzinfo=UTC)
SESSION = date(2026, 9, 24)
CONFIG = M2CarryGateConfig(
    schema_version="1",
    min_remaining_dte=2,
    overnight_loss_budget_fraction=Decimal("0.03"),
)
REFERENCE = Money.of("140000.00", Currency.INR)


def _market(*, trend: TrendState = TrendState.UP) -> MarketState:
    return MarketState(
        market_state_id="MKT-P9",
        feature_version="p9-v1",
        calculated_at=NOW,
        source_snapshot_ids=("SNAP-1",),
        trend=trend,
        volatility=VolatilityState.NORMAL,
        return_15m=Decimal("0.004"),
        return_60m=Decimal("0.012"),
        normalized_return_15m=Decimal("0.7"),
        normalized_return_60m=Decimal("0.8"),
        normalized_vwap_distance=Decimal("0.5"),
        realized_volatility_ratio=Decimal("1.0"),
        realized_volatility_annualized=Decimal("15"),
        iv_percentile=Decimal("30"),
        iv_rv_ratio=Decimal("1.0"),
        trend_score=Decimal("0.7"),
        event_state="NORMAL",
        macro_status=MacroStatus.NEUTRAL,
        quality=DataQuality.VALID,
        warmup_complete=True,
        completed_bar_count=60,
        session_count=1,
    )


def _setup_features(
    *, trend: TrendState = TrendState.UP, dte: int = 5
) -> SetupFeatures:
    return SetupFeatures(
        identification_rule_version="p9-v1",
        router_version="p9-v1",
        market_state_id="MKT-P9",
        raw_setup_score=Decimal("0.7"),
        score_components={"binding": Decimal("0.7")},
        trend=trend,
        volatility=VolatilityState.NORMAL,
        structure=StructureKind.LONG_OPTION,
        dte=dte,
        delta=Decimal("0.5"),
        spread_fraction=Decimal("0.01"),
        liquidity_rank=Decimal("0.8"),
        event_state="NORMAL",
        macro_status=MacroStatus.NEUTRAL,
    )


def _intent(*, family: FamilyId = FamilyId.long_call) -> TradeIntent:
    contract = f.option_contract(
        symbol="NIFTY26OCT24000CE",
        strike=Decimal("24000"),
        expiry=date(2026, 10, 1),
    )
    return f.intent(
        strategy_id="positional_long_option",
        strategy_version="1.0.0",
        experiment_id="EXP-M2-P9",
        mode_id=ModeId.M2_DIRECTIONAL,
        family_id=family.value,
        legs=(
            IntentLeg(
                leg_id="LEG-1",
                contract=contract,
                side=Side.BUY,
                ratio=1,
            ),
        ),
        estimated_max_loss=f.money("3000.00"),
        requested_risk=f.money("3000.00"),
        setup_features=_setup_features(),
        created_at=NOW - timedelta(hours=2),
        expires_at=NOW + timedelta(minutes=15),
    )


def _position(intent: TradeIntent) -> PositionState:
    return f.position_state(
        trade_id="TRD-P9-001",
        intent_id=intent.intent_id,
        strategy_id=intent.strategy_id,
        strategy_version=intent.strategy_version,
        experiment_id=intent.experiment_id,
        mode_id=ModeId.M2_DIRECTIONAL,
        legs=(
            f.position_leg_state(
                leg_id="LEG-1",
                contract=intent.legs[0].contract,
                average_entry_price=f.price("100.00"),
                current_stop_price=f.price("98.00"),
            ),
        ),
        exit_policy=f.exit_policy(
            trade_id="TRD-P9-001",
            stop_price=f.price("98.00"),
        ),
        opened_at=NOW - timedelta(hours=2),
        as_of=NOW,
        protective_order_ids=("PROT-P9-1",),
    )


def _approved_inputs(**overrides: object) -> M2CarryGateInput:
    payload = {
        "mode_id": ModeId.M2_DIRECTIONAL,
        "thesis_evaluated_today": True,
        "thesis_still_valid": True,
        "remaining_dte": 5,
        "overnight_max_loss": Money.of("3000.00", Currency.INR),
        "overnight_loss_budget": Money.of("4200.00", Currency.INR),
        "event_blackout": False,
        "portfolio_entries_blocked": False,
        "mode_daily_loss_breached": False,
        "exit_policy_persisted": True,
        "recovery_healthy": True,
        "current_pnl": Money.of("-500.00", Currency.INR),
    }
    payload.update(overrides)
    return M2CarryGateInput.model_validate(payload)


class TestEvaluateM2CarryGate:
    def test_carry_approved_when_all_evidence_passes(self) -> None:
        decision = evaluate_m2_carry_gate(
            trade_id="TRD-P9-001",
            session_date=SESSION,
            inputs=_approved_inputs(),
            config=CONFIG,
            as_of=NOW,
            decision_id="CRG-APPROVE",
        )
        assert decision.action is CarryGateAction.CARRY_APPROVED
        assert decision.mode_id is ModeId.M2_DIRECTIONAL
        assert decision.reason_code is ReasonCode.OK
        assert decision.exit_initiated is False

    def test_losing_trade_can_carry_when_evidence_passes(self) -> None:
        """T19: a loss alone does not reject when all evidence still passes."""
        decision = evaluate_m2_carry_gate(
            trade_id="TRD-P9-LOSS",
            session_date=SESSION,
            inputs=_approved_inputs(current_pnl=Money.of("-2500.00", Currency.INR)),
            config=CONFIG,
            as_of=NOW,
            decision_id="CRG-LOSS",
        )
        assert decision.action is CarryGateAction.CARRY_APPROVED

    def test_profit_alone_does_not_approve_when_thesis_missing(self) -> None:
        """T18/T19: profit without fresh thesis evidence is rejected."""
        decision = evaluate_m2_carry_gate(
            trade_id="TRD-P9-PROFIT",
            session_date=SESSION,
            inputs=_approved_inputs(
                thesis_evaluated_today=False,
                current_pnl=Money.of("5000.00", Currency.INR),
            ),
            config=CONFIG,
            as_of=NOW,
            decision_id="CRG-PROFIT",
        )
        assert decision.action is CarryGateAction.CARRY_REJECTED
        assert decision.reason_code is ReasonCode.CARRY_THESIS_STALE
        assert decision.exit_initiated is True

    def test_rejected_carry_flags_exit_initiation(self) -> None:
        decision = evaluate_m2_carry_gate(
            trade_id="TRD-P9-REJECT",
            session_date=SESSION,
            inputs=_approved_inputs(event_blackout=True),
            config=CONFIG,
            as_of=NOW,
            decision_id="CRG-REJECT",
        )
        assert decision.action is CarryGateAction.CARRY_REJECTED
        assert decision.reason_code is ReasonCode.CARRY_EVENT_BLACKOUT
        assert decision.exit_initiated is True
        assert decision.mode_id is ModeId.M2_DIRECTIONAL

    def test_wrong_mode_is_rejected_without_exit(self) -> None:
        decision = evaluate_m2_carry_gate(
            trade_id="TRD-P9-M3",
            session_date=SESSION,
            inputs=_approved_inputs(mode_id=ModeId.M3_TACTICAL_POSITIONAL),
            config=CONFIG,
            as_of=NOW,
            decision_id="CRG-M3",
        )
        assert decision.action is CarryGateAction.CARRY_REJECTED
        assert decision.reason_code is ReasonCode.CARRY_MODE_MISMATCH
        assert decision.exit_initiated is False


class TestBuildM2CarryGateInput:
    def test_builds_from_open_m2_position(self) -> None:
        intent = _intent()
        position = _position(intent)
        inputs = build_m2_carry_gate_input(
            position=position,
            intent=intent,
            market=_market(trend=TrendState.UP),
            session_date=SESSION,
            mode_reference_capital=REFERENCE,
            config=CONFIG,
            event_blackout=False,
            portfolio_entries_blocked=False,
            mode_daily_loss_breached=False,
            recovery_healthy=True,
            remaining_dte=5,
        )
        assert inputs.mode_id is ModeId.M2_DIRECTIONAL
        assert inputs.thesis_evaluated_today is True
        assert inputs.thesis_still_valid is True
        assert inputs.overnight_max_loss == intent.estimated_max_loss

    def test_thesis_invalid_for_misaligned_put(self) -> None:
        intent = _intent(family=FamilyId.long_put)
        position = _position(intent)
        inputs = build_m2_carry_gate_input(
            position=position,
            intent=intent,
            market=_market(trend=TrendState.UP),
            session_date=SESSION,
            mode_reference_capital=REFERENCE,
            config=CONFIG,
            event_blackout=False,
            portfolio_entries_blocked=False,
            mode_daily_loss_breached=False,
            recovery_healthy=True,
            remaining_dte=5,
        )
        assert inputs.thesis_still_valid is False


class TestAssessM2Thesis:
    def test_long_call_valid_on_up_trend(self) -> None:
        intent = _intent(family=FamilyId.long_call)
        fresh, valid = assess_m2_thesis(intent, market=_market(), session_date=SESSION)
        assert fresh is True
        assert valid is True

    def test_missing_market_is_not_fresh(self) -> None:
        intent = _intent()
        fresh, valid = assess_m2_thesis(intent, market=None, session_date=SESSION)
        assert fresh is False
        assert valid is False


class TestLegacyPathIsolation:
    def test_legacy_profit_carry_is_not_the_mode2_gate(self) -> None:
        """Legacy evaluate_carry_forward still requires profit; P9 path does not."""
        legacy = evaluate_carry_forward(
            symbol="NIFTY26OCT24000CE",
            current_pnl=Decimal("-1000"),
            conviction_at_close=ConvictionAssessment(
                assessment_id="conv-1",
                symbol="NIFTY26OCT24000CE",
                direction=TradeDirection.LONG,
                conviction_level=ConvictionLevel.VERY_STRONG,
                conviction_score=Decimal("90"),
                trend_alignment_score=Decimal("90"),
                volume_confirmation_score=Decimal("80"),
                oi_confirmation_score=Decimal("80"),
                price_action_score=Decimal("80"),
                sector_support_score=Decimal("80"),
                regime_alignment_score=Decimal("90"),
                assessed_at=NOW,
            ),
            regime_aligned=True,
            min_conviction_for_carry=Decimal("65"),
            as_of=NOW,
            decision_id="legacy-1",
        )
        modern = evaluate_m2_carry_gate(
            trade_id="TRD-P9-001",
            session_date=SESSION,
            inputs=_approved_inputs(current_pnl=Money.of("-1000.00", Currency.INR)),
            config=CONFIG,
            as_of=NOW,
            decision_id="modern-1",
        )
        assert legacy.action is CarryForwardAction.CLOSE
        assert modern.action is CarryGateAction.CARRY_APPROVED


def test_carry_gate_config_loads() -> None:
    config = load_carry_gate_config()
    assert config.min_remaining_dte >= 0
    assert config.overnight_loss_budget_fraction > 0
