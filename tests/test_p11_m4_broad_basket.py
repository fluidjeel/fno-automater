"""Phase P11 M4 Broad Basket: butterflies, straddle, strangle, iron butterfly.

Locks in Phase P11 requirements from:
- FOUR_MODE_LAYER_CHANGE_CRITERIA.md (§3.10)
- NIFTY_FOUR_MODE_CURSOR_REDESIGN.md (§5, T05, T22, T23)
- FOUR_MODE_REDESIGN_PLAN.md (Phase P11)
- REQUIREMENT_TRACEABILITY.md (R-004, R-011, T05, T22, T23)
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from trading.broker.ports import MarginPreviewRequest, MarginPreviewResult
from trading.config import load_config, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    Greeks,
    InstrumentSpec,
    MarketState,
    RiskDecision,
    TradeIntent,
)
from trading.domain.contracts.identification import (
    MacroStatus,
    TrendState,
    VolatilityState,
)
from trading.domain.enums import (
    DataQuality,
    Exchange,
    ExecutionMode,
    FamilyId,
    InstrumentKind,
    ModeId,
    OptionType,
    OrderState,
    RiskAction,
    Side,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory
from trading.identification import (
    bind_long_call_butterfly,
    bind_long_straddle,
    bind_long_strangle,
    bind_short_iron_butterfly,
    load_identification_policy,
)
from trading.oms import OrderPlanPlanner, OrderPlanRequest
from trading.risk import CapitalReservationService, RiskGateway, RiskGatewayRequest
from trading.risk.payoff import (
    PayoffLeg,
    PayoffStatus,
    evaluate_same_expiry_payoff,
    formula_short_iron_butterfly_defined,
)
from trading.risk.sizing.butterfly import butterfly_legs, is_long_butterfly
from trading.risk.sizing.iron_butterfly import iron_butterfly_legs, is_iron_butterfly
from trading.risk.sizing.long_volatility import is_long_straddle, is_long_strangle
from trading.runtime.paper_session import PaperSessionConfig
from trading.runtime.startup_validation import (
    StartupValidationError,
    validate_startup_configuration,
)
from trading.storage.trading_store import TradingStore
from trading.strategies.base import StrategyContext
from trading.strategies.m4_broad_basket import (
    LongCallButterflyStrategy,
    LongPutButterflyStrategy,
    LongStraddleStrategy,
    LongStrangleStrategy,
    ShortIronButterflyStrategy,
)
from trading.strategies.macro import MacroAssessment, MacroBias
from trading.trade import TradeManager

ROOT = Path(__file__).resolve().parent.parent
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
IDENTIFICATION_POLICY = load_identification_policy(
    ROOT / "config" / "identification.yaml"
)
ACCOUNT_ID = "ACC-PAPER-1"
NOW = f.NOW
EXPIRY = date(2026, 10, 1)


class ConfirmedMarginPreview:
    def preview_margin(self, request: MarginPreviewRequest) -> MarginPreviewResult:
        return MarginPreviewResult(
            request_id=request.request_id,
            as_of=NOW,
            margin_required=f.money("5000"),
            margin_available_after=f.money("5000000"),
            confirmed=True,
        )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW + timedelta(seconds=60))


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper_p11.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _market(
    *,
    trend: TrendState = TrendState.RANGE,
    volatility: VolatilityState = VolatilityState.EXPANDING,
) -> MarketState:
    return MarketState.model_validate(
        {
            "market_state_id": "market-p11",
            "feature_version": IDENTIFICATION_POLICY.feature_version,
            "calculated_at": NOW,
            "source_snapshot_ids": ("snapshot-1",),
            "trend": trend,
            "volatility": volatility,
            "return_15m": Decimal("0.001"),
            "return_60m": Decimal("0.002"),
            "normalized_return_15m": Decimal("0.1"),
            "normalized_return_60m": Decimal("0.1"),
            "normalized_vwap_distance": Decimal("0.1"),
            "realized_volatility_ratio": Decimal("1.0"),
            "realized_volatility_annualized": Decimal("15"),
            "iv_percentile": Decimal("55"),
            "iv_rv_ratio": Decimal("1.0"),
            "trend_score": Decimal("0.05"),
            "event_state": "NORMAL",
            "macro_status": MacroStatus.NEUTRAL,
            "quality": DataQuality.VALID,
            "warmup_complete": True,
            "completed_bar_count": 60,
            "session_count": 20,
        }
    )


def _macro() -> MacroAssessment:
    return MacroAssessment(
        regime="NEUTRAL_VOLATILITY",
        directional_bias=MacroBias.NEUTRAL,
        confidence=Decimal("0.8"),
        fresh_until=NOW + timedelta(hours=1),
        evidence_ids=("src-1",),
        model_version="macro-v1",
    )


def _option(
    symbol: str,
    *,
    strike: str,
    delta: str,
    option_type: OptionType,
    bid: str = "20.00",
    ask: str = "20.10",
) -> FeatureSnapshot:
    return f.snapshot(
        contract=f.option_contract(
            symbol=symbol,
            strike=Decimal(strike),
            option_type=option_type,
            expiry=EXPIRY,
        ),
        market=f.quote(bid=f.price(bid), ask=f.price(ask), bid_size=500, ask_size=500),
        features={"lot_size": Decimal(65), "top_of_book_observed": Decimal(1)},
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=option_type,
            greeks=Greeks(
                model="fixture",
                calculation_version="1",
                converged=True,
                implied_volatility=Decimal("15"),
                delta=Decimal(delta),
            ),
            underlying_price=f.price("24500"),
        ),
    )


def _instrument_spec(
    symbol: str, strike: Decimal, option_type: OptionType
) -> InstrumentSpec:
    return InstrumentSpec.model_validate(
        {
            "trading_symbol": f"NSE:{symbol}",
            "exchange": Exchange.NFO,
            "segment": "NSE_FO",
            "underlying": "NIFTY",
            "instrument_kind": InstrumentKind.OPTION,
            "provider_token": "tok-1",
            "exchange_token": 1,
            "lot_size": 65,
            "tick_size": Decimal("0.05"),
            "price_precision": 2,
            "expiry": EXPIRY,
            "strike": strike,
            "option_type": option_type,
            "trading_session": "0915-1530",
            "source": "fixture",
            "verified_at": date(2026, 9, 1),
        }
    )


def _ctx(candidates: tuple[FeatureSnapshot, ...]) -> StrategyContext:
    return StrategyContext(
        underlying=f.snapshot(
            snapshot_id="SNAP-1",
            contract=f.index_contract(),
            market=f.quote(last=f.price("24500"), close=f.price("24500")),
        ),
        candidates=candidates,
        view=f.portfolio_view(),
        now=NOW,
        macro=_macro(),
    )


def _call_butterfly_candidates() -> tuple[FeatureSnapshot, ...]:
    return (
        _option(
            "NIFTY26OCT24460CE",
            strike="24460",
            delta="0.35",
            option_type=OptionType.CALL,
            bid="29.00",
            ask="29.10",
        ),
        _option(
            "NIFTY26OCT24500CE",
            strike="24500",
            delta="0.50",
            option_type=OptionType.CALL,
            bid="9.00",
            ask="9.10",
        ),
        _option(
            "NIFTY26OCT24540CE",
            strike="24540",
            delta="0.15",
            option_type=OptionType.CALL,
            bid="4.00",
            ask="4.10",
        ),
    )


def _put_butterfly_candidates() -> tuple[FeatureSnapshot, ...]:
    return (
        _option(
            "NIFTY26OCT24460PE",
            strike="24460",
            delta="-0.15",
            option_type=OptionType.PUT,
            bid="4.00",
            ask="4.10",
        ),
        _option(
            "NIFTY26OCT24500PE",
            strike="24500",
            delta="-0.50",
            option_type=OptionType.PUT,
            bid="9.00",
            ask="9.10",
        ),
        _option(
            "NIFTY26OCT24540PE",
            strike="24540",
            delta="-0.35",
            option_type=OptionType.PUT,
            bid="29.00",
            ask="29.10",
        ),
    )


def _iron_butterfly_candidates() -> tuple[FeatureSnapshot, ...]:
    return (
        _option(
            "NIFTY26OCT24460PE",
            strike="24460",
            delta="-0.15",
            option_type=OptionType.PUT,
            bid="8.00",
            ask="8.05",
        ),
        _option(
            "NIFTY26OCT24500PE",
            strike="24500",
            delta="-0.50",
            option_type=OptionType.PUT,
            bid="19.95",
            ask="20.05",
        ),
        _option(
            "NIFTY26OCT24500CE",
            strike="24500",
            delta="0.50",
            option_type=OptionType.CALL,
            bid="19.95",
            ask="20.05",
        ),
        _option(
            "NIFTY26OCT24540CE",
            strike="24540",
            delta="0.15",
            option_type=OptionType.CALL,
            bid="8.00",
            ask="8.05",
        ),
    )


def _straddle_candidates() -> tuple[FeatureSnapshot, ...]:
    return (
        _option(
            "NIFTY26OCT24500PE",
            strike="24500",
            delta="-0.50",
            option_type=OptionType.PUT,
            bid="90.00",
            ask="90.10",
        ),
        _option(
            "NIFTY26OCT24500CE",
            strike="24500",
            delta="0.50",
            option_type=OptionType.CALL,
            bid="90.00",
            ask="90.10",
        ),
    )


def _strangle_candidates() -> tuple[FeatureSnapshot, ...]:
    return (
        _option(
            "NIFTY26OCT24460PE",
            strike="24460",
            delta="-0.30",
            option_type=OptionType.PUT,
            bid="30.00",
            ask="30.10",
        ),
        _option(
            "NIFTY26OCT24540CE",
            strike="24540",
            delta="0.30",
            option_type=OptionType.CALL,
            bid="30.00",
            ask="30.10",
        ),
    )


def _gateway_decision(
    intent: TradeIntent,
    candidates: tuple[FeatureSnapshot, ...],
    *,
    store: TradingStore,
    clock: FrozenClock,
) -> RiskDecision:
    ids = SequentialIdFactory(clock.instant)
    gateway = RiskGateway(
        risk_policy=RISK_POLICY,
        account_config=ACCOUNT_CONFIG,
        reservation_service=CapitalReservationService(
            store, clock=clock, id_factory=ids
        ),
        margin_preview=ConfirmedMarginPreview(),
        clock=clock,
        id_factory=ids,
        nifty_only_execution=True,
    )
    by_symbol = {item.contract.symbol: item for item in candidates}
    leg_snapshots = {leg.leg_id: by_symbol[leg.contract.symbol] for leg in intent.legs}
    return gateway.evaluate(
        RiskGatewayRequest(
            intent=intent,
            feature_snapshot=candidates[0],
            portfolio_snapshot=f.portfolio_snapshot(
                exposure=f.exposure(
                    equity=f.money("5000000"),
                    margin_available=f.money("5000000"),
                )
            ),
            instrument=_instrument_spec(
                candidates[0].contract.symbol,
                candidates[0].contract.strike or Decimal("24500"),
                candidates[0].contract.option_type or OptionType.CALL,
            ),
            leg_snapshots=leg_snapshots,
            event_risk_state=f.event_risk_state(),
        )
    )


class TestP11Payoff:
    def test_long_call_butterfly_formula_matches_generic(self) -> None:
        legs = [
            PayoffLeg(
                strike=Decimal("24460"),
                option_type=OptionType.CALL,
                side=Side.BUY,
                premium=Decimal("29"),
            ),
            PayoffLeg(
                strike=Decimal("24500"),
                option_type=OptionType.CALL,
                side=Side.SELL,
                premium=Decimal("9"),
                quantity=2,
            ),
            PayoffLeg(
                strike=Decimal("24540"),
                option_type=OptionType.CALL,
                side=Side.BUY,
                premium=Decimal("4"),
            ),
        ]
        report = evaluate_same_expiry_payoff(
            legs,
            lot_size=65,
            family_id=FamilyId.long_call_butterfly,
        )
        assert report.status is PayoffStatus.IMPLEMENTED_UNIT
        assert report.formula_agrees is True

    def test_short_iron_butterfly_formula_matches_generic(self) -> None:
        legs = [
            PayoffLeg(
                strike=Decimal("24460"),
                option_type=OptionType.PUT,
                side=Side.BUY,
                premium=Decimal("8"),
            ),
            PayoffLeg(
                strike=Decimal("24500"),
                option_type=OptionType.PUT,
                side=Side.SELL,
                premium=Decimal("20"),
            ),
            PayoffLeg(
                strike=Decimal("24500"),
                option_type=OptionType.CALL,
                side=Side.SELL,
                premium=Decimal("19"),
            ),
            PayoffLeg(
                strike=Decimal("24540"),
                option_type=OptionType.CALL,
                side=Side.BUY,
                premium=Decimal("8"),
            ),
        ]
        formula_loss, _ = formula_short_iron_butterfly_defined(
            long_put_strike=Decimal("24460"),
            short_strike=Decimal("24500"),
            long_call_strike=Decimal("24540"),
            long_put_premium=Decimal("8"),
            short_put_premium=Decimal("20"),
            short_call_premium=Decimal("19"),
            long_call_premium=Decimal("8"),
            lot_size=65,
        )
        report = evaluate_same_expiry_payoff(
            legs,
            lot_size=65,
            family_id=FamilyId.short_iron_butterfly_defined,
        )
        assert report.formula_agrees is True
        assert report.generic_max_loss == formula_loss


class TestP11StrategiesAndBinders:
    def test_long_call_butterfly_stamps_m4_and_long_before_short(self) -> None:
        candidates = _call_butterfly_candidates()
        intent = LongCallButterflyStrategy().evaluate(_ctx(candidates)).intents[0]
        assert intent.mode_id is ModeId.M4_STRATEGIC_POSITIONAL
        assert intent.family_id == FamilyId.long_call_butterfly.value
        assert is_long_butterfly(intent)
        legs = butterfly_legs(intent)
        assert legs.low_wing.side is Side.BUY
        assert legs.short_body.side is Side.SELL
        assert legs.short_body.ratio == 2

    def test_bind_long_call_butterfly_on_range(self) -> None:
        bound = bind_long_call_butterfly(
            _call_butterfly_candidates(),
            market=_market(),
            policy=IDENTIFICATION_POLICY,
        )
        assert bound.binding.eligible is True
        assert bound.binding.strategy_id == "long_call_butterfly"

    def test_short_iron_butterfly_prefix_safe_order(self) -> None:
        candidates = _iron_butterfly_candidates()
        intent = ShortIronButterflyStrategy().evaluate(_ctx(candidates)).intents[0]
        assert is_iron_butterfly(intent)
        iron_butterfly_legs(intent)
        assert intent.legs[0].side is Side.BUY
        assert intent.legs[1].side is Side.BUY
        assert intent.legs[2].side is Side.SELL

    def test_bind_short_iron_butterfly_long_first(self) -> None:
        bound = bind_short_iron_butterfly(
            _iron_butterfly_candidates(),
            market=_market(),
            policy=IDENTIFICATION_POLICY,
        )
        assert bound.binding.eligible is True
        assert bound.candidates[0].contract.option_type is OptionType.PUT
        assert bound.candidates[1].contract.option_type is OptionType.CALL

    def test_long_straddle_is_debit_only(self) -> None:
        intent = (
            LongStraddleStrategy().evaluate(_ctx(_straddle_candidates())).intents[0]
        )
        assert all(leg.side is Side.BUY for leg in intent.legs)
        assert is_long_straddle(intent)

    def test_long_strangle_is_debit_only(self) -> None:
        intent = (
            LongStrangleStrategy().evaluate(_ctx(_strangle_candidates())).intents[0]
        )
        assert all(leg.side is Side.BUY for leg in intent.legs)
        assert is_long_strangle(intent)

    def test_bind_straddle_abstains_on_compressed_vol(self) -> None:
        bound = bind_long_straddle(
            _straddle_candidates(),
            market=_market(volatility=VolatilityState.COMPRESSED),
            policy=IDENTIFICATION_POLICY,
        )
        assert bound.binding.eligible is False

    def test_bind_strangle_on_expanding_vol(self) -> None:
        bound = bind_long_strangle(
            _strangle_candidates(),
            market=_market(volatility=VolatilityState.EXPANDING),
            policy=IDENTIFICATION_POLICY,
        )
        assert bound.binding.eligible is True


class TestP11GatewayPrefixSafety:
    def test_iron_butterfly_long_before_short(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        candidates = _iron_butterfly_candidates()
        intent = ShortIronButterflyStrategy().evaluate(_ctx(candidates)).intents[0]
        decision = _gateway_decision(intent, candidates, store=store, clock=clock)
        assert decision.action in {RiskAction.APPROVE, RiskAction.RESIZE}
        assert [leg.leg_id for leg in decision.approved_legs] == [
            "leg-long-put",
            "leg-long-call",
            "leg-short-put",
            "leg-short-call",
        ]

    def test_call_butterfly_long_wings_before_body(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        candidates = _call_butterfly_candidates()
        intent = LongCallButterflyStrategy().evaluate(_ctx(candidates)).intents[0]
        decision = _gateway_decision(intent, candidates, store=store, clock=clock)
        assert decision.action in {RiskAction.APPROVE, RiskAction.RESIZE}
        assert [leg.leg_id for leg in decision.approved_legs] == [
            "leg-low-wing",
            "leg-high-wing",
            "leg-short-body",
        ]


class TestP11G2Lifecycle:
    def test_call_butterfly_g2_entry_and_partial_fill_repair(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        ids = SequentialIdFactory(clock.instant)
        manager = TradeManager(clock=clock, id_factory=ids)
        candidates = _call_butterfly_candidates()
        intent = LongCallButterflyStrategy().evaluate(_ctx(candidates)).intents[0]
        decision = f.risk_decision(
            intent_id=intent.intent_id,
            approved_legs=tuple(f.approved_leg(leg.leg_id) for leg in intent.legs),
        )
        by_symbol = {item.contract.symbol: item for item in candidates}
        leg_snapshots = {
            leg.leg_id: by_symbol[leg.contract.symbol] for leg in intent.legs
        }
        plan = OrderPlanPlanner(clock=clock, id_factory=ids).build(
            OrderPlanRequest(
                intent=intent,
                decision=decision,
                feature_snapshot=candidates[0],
                account_id=ACCOUNT_ID,
                leg_snapshots=leg_snapshots,
            )
        )
        trade_id = manager.begin_entry(intent, plan)
        short_order = next(
            order for order in plan.orders if order.leg_id == "leg-short-body"
        )
        partial = f.order_event(
            state=OrderState.PARTIAL,
            filled_quantity=20,
            average_fill_price=short_order.command.limit_price,
            identity=f.order_identity(
                trade_id=trade_id, broker_order_id="BRK-FLY-PART"
            ),
            command=short_order.command,
        )
        repaired = manager.apply_order_event(partial, intent=intent)
        assert repaired.state is TradeState.REPAIR_REQUIRED
        assert repaired.state is not TradeState.OPEN

    def test_put_butterfly_stamps_m4(self) -> None:
        intent = (
            LongPutButterflyStrategy()
            .evaluate(_ctx(_put_butterfly_candidates()))
            .intents[0]
        )
        assert intent.mode_id is ModeId.M4_STRATEGIC_POSITIONAL
        assert intent.family_id == FamilyId.long_put_butterfly.value


class TestP11StartupValidation:
    def _session(self, **stances: ExecutionMode) -> PaperSessionConfig:
        return PaperSessionConfig.model_validate(
            {
                "poll_interval_seconds": 60,
                "eod_local": "15:40",
                "option_strikes_each_side": 2,
                "experiment_prefix": "EXP-TEST",
                "strategy_ids": tuple(stances),
                "strategy_stances": stances,
                "commodity_underlying": "CRUDEOIL",
                "commodity_exchange": "MCX",
                "commodity_segment": "MCX_COM",
                "cohort_dir": "data/paper/cohorts",
                "store_path": "data/paper/trading.sqlite",
                "broker_state_path": "data/paper/broker_state.json",
            }
        )

    def test_affordable_butterfly_families_may_run_paper_after_g2(self) -> None:
        cfg = self._session(
            long_call_butterfly=ExecutionMode.PAPER,
            long_put_butterfly=ExecutionMode.SHADOW,
        )
        validated, warnings = validate_startup_configuration(cfg)
        assert validated.strategy_stances["long_call_butterfly"] is ExecutionMode.PAPER
        assert validated.strategy_stances["long_put_butterfly"] is ExecutionMode.SHADOW
        assert warnings == []

    def test_straddle_and_strangle_blocked_by_g1(self) -> None:
        with pytest.raises(
            StartupValidationError, match=r"Gate G1 violation.*long_straddle"
        ):
            validate_startup_configuration(
                self._session(long_straddle=ExecutionMode.PAPER)
            )
        with pytest.raises(
            StartupValidationError, match=r"Gate G1 violation.*long_strangle"
        ):
            validate_startup_configuration(
                self._session(long_strangle=ExecutionMode.PAPER)
            )

    def test_turning_one_family_paper_does_not_turn_others(self) -> None:
        cfg = self._session(
            long_call_butterfly=ExecutionMode.PAPER,
            short_iron_butterfly_defined=ExecutionMode.SHADOW,
            long_straddle=ExecutionMode.SHADOW,
        )
        validated, _ = validate_startup_configuration(cfg)
        assert validated.strategy_stances["long_call_butterfly"] is ExecutionMode.PAPER
        assert (
            validated.strategy_stances["short_iron_butterfly_defined"]
            is ExecutionMode.SHADOW
        )
        assert validated.strategy_stances["long_straddle"] is ExecutionMode.SHADOW

    def test_no_short_straddle_or_strangle_family(self) -> None:
        assert not hasattr(FamilyId, "short_straddle")
        assert not hasattr(FamilyId, "short_strangle")
