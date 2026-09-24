"""Phase P10 Iron Condor Binder, Prefix Safety, and G2 Test Suite.

Locks in Phase P10 requirements from:
- FOUR_MODE_LAYER_CHANGE_CRITERIA.md (§2.1, §2.4, §3.9)
- NIFTY_FOUR_MODE_CURSOR_REDESIGN.md (§5, §10)
- FOUR_MODE_REDESIGN_PLAN.md (Phase P10)
- REQUIREMENT_TRACEABILITY.md (R-004, R-011, T04, T22)

Verifications:
1. Wider-wing loss formula matches generic kink/slope payoff (equal and asymmetric wings).
2. Gateway approved leg order is long protection before short (prefix-safe).
3. IronCondorStrategy stamps ModeId.M4 and FamilyId.short_iron_condor_defined.
4. bind_iron_condor selects four legs in long-first order for RANGE markets.
5. G2 lifecycle: entry, partial fill repair, monitoring, exit, restart.
6. short_iron_condor_defined may run PAPER after G2; legacy iron_condor id stays SHADOW-only.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from trading.broker.paper import PaperBroker
from trading.broker.ports import MarginPreviewRequest, MarginPreviewResult
from trading.config import load_config, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    Greeks,
    InstrumentSpec,
    MarketState,
    OrderCommand,
    OrderEvent,
    OrderIdentity,
    OrderPlan,
    PlannedOrder,
    RiskDecision,
    TradeIntent,
)
from trading.domain.contracts.identification import (
    MacroStatus,
    TrendState,
    VolatilityState,
)
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.enums import (
    DataQuality,
    Exchange,
    ExecutionMode,
    FamilyId,
    HoldingStyle,
    InstrumentKind,
    ModeId,
    OptionType,
    OrderPlanState,
    OrderState,
    OrderType,
    RiskAction,
    Side,
    TimeInForce,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory, derive_idempotency_key
from trading.identification import bind_iron_condor, load_identification_policy
from trading.oms import OmsEngine, OrderPlanPlanner, OrderPlanRequest, OrderRateLimiter
from trading.risk import CapitalReservationService, RiskGateway, RiskGatewayRequest
from trading.risk.payoff import (
    PayoffLeg,
    PayoffStatus,
    evaluate_same_expiry_payoff,
    formula_short_iron_condor_defined,
)
from trading.risk.sizing.iron_condor import condor_legs
from trading.runtime.paper_session import PaperSessionConfig
from trading.runtime.startup_validation import (
    StartupValidationError,
    validate_startup_configuration,
)
from trading.storage.trading_store import TradingStore
from trading.strategies.base import StrategyContext
from trading.strategies.iron_condor import IronCondorStrategy
from trading.strategies.macro import MacroAssessment, MacroBias
from trading.trade import TradeManager

ROOT = Path(__file__).resolve().parent.parent
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
IDENTIFICATION_POLICY = load_identification_policy(
    ROOT / "config" / "identification.yaml"
)
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
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
    trading_store = TradingStore.open(tmp_path / "paper_p10.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _market(trend: TrendState = TrendState.RANGE) -> MarketState:
    return MarketState.model_validate(
        {
            "market_state_id": "market-p10",
            "feature_version": IDENTIFICATION_POLICY.feature_version,
            "calculated_at": NOW,
            "source_snapshot_ids": ("snapshot-1",),
            "trend": trend,
            "volatility": VolatilityState.NORMAL,
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
        features={
            "lot_size": Decimal(65),
            "top_of_book_observed": Decimal(1),
        },
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


def _condor_candidates() -> tuple[FeatureSnapshot, ...]:
    # 40-point wings keep one-lot max loss under the M4 per-trade cap (₹2,800).
    return (
        _option(
            "NIFTY26OCT24400PE",
            strike="24400",
            delta="-0.15",
            option_type=OptionType.PUT,
            bid="8.00",
            ask="8.10",
        ),
        _option(
            "NIFTY26OCT24440PE",
            strike="24440",
            delta="-0.28",
            option_type=OptionType.PUT,
            bid="15.00",
            ask="15.10",
        ),
        _option(
            "NIFTY26OCT24560CE",
            strike="24560",
            delta="0.28",
            option_type=OptionType.CALL,
            bid="15.00",
            ask="15.10",
        ),
        _option(
            "NIFTY26OCT24600CE",
            strike="24600",
            delta="0.15",
            option_type=OptionType.CALL,
            bid="8.00",
            ask="8.10",
        ),
    )


def _adverse_exit_snapshots(
    legs: object,
    candidates: tuple[FeatureSnapshot, ...],
) -> dict[str, FeatureSnapshot]:
    """Whole-structure marks that breach the 40-tick strategy P&L stop."""
    return {
        legs.long_put.leg_id: _option(
            candidates[0].contract.symbol,
            strike=str(candidates[0].contract.strike),
            delta="-0.20",
            option_type=OptionType.PUT,
            bid="2.00",
            ask="2.10",
        ),
        legs.short_put.leg_id: _option(
            candidates[1].contract.symbol,
            strike=str(candidates[1].contract.strike),
            delta="-0.50",
            option_type=OptionType.PUT,
            bid="70.00",
            ask="70.10",
        ),
        legs.short_call.leg_id: _option(
            candidates[2].contract.symbol,
            strike=str(candidates[2].contract.strike),
            delta="0.50",
            option_type=OptionType.CALL,
            bid="70.00",
            ask="70.10",
        ),
        legs.long_call.leg_id: _option(
            candidates[3].contract.symbol,
            strike=str(candidates[3].contract.strike),
            delta="0.20",
            option_type=OptionType.CALL,
            bid="2.00",
            ask="2.10",
        ),
    }


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


def _build_exit_plan(
    *,
    intent: TradeIntent,
    decision: RiskDecision,
    entry_events: tuple[OrderEvent, ...],
    leg_snapshots: dict[str, FeatureSnapshot],
    account_id: str,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> OrderPlan:
    trade_id = entry_events[0].identity.trade_id
    planned_orders: list[PlannedOrder] = []
    for leg in intent.legs:
        entry = next(
            event
            for event in entry_events
            if event.command.contract.symbol == leg.contract.symbol
        )
        feature = leg_snapshots[leg.leg_id]
        exit_side = Side.SELL if leg.side is Side.BUY else Side.BUY
        reference = feature.market.bid if exit_side is Side.SELL else feature.market.ask
        assert reference is not None
        internal_order_id = id_factory.new_id("ORD")
        planned_orders.append(
            PlannedOrder(
                plan_leg_id=f"{leg.leg_id}-exit",
                leg_id=leg.leg_id,
                identity=OrderIdentity(
                    internal_order_id=internal_order_id,
                    client_order_id=internal_order_id,
                    idempotency_key=derive_idempotency_key(
                        account_id=account_id,
                        strategy_id=intent.strategy_id,
                        strategy_version=intent.strategy_version,
                        intent_id=intent.intent_id,
                        leg_id=f"{leg.leg_id}-exit",
                        side=exit_side.value,
                        quantity_contracts=entry.filled_quantity,
                    ),
                    intent_id=intent.intent_id,
                    risk_decision_id=decision.decision_id,
                    trade_id=trade_id,
                    correlation_id=intent.correlation_id,
                    experiment_id=intent.experiment_id,
                    execution_mode=intent.execution_mode,
                ),
                command=OrderCommand(
                    contract=leg.contract,
                    side=exit_side,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.DAY,
                    quantity_contracts=entry.filled_quantity,
                    limit_price=reference,
                ),
                plan_state=OrderPlanState.RISK_APPROVED,
            )
        )
    now = clock.now_utc()
    return OrderPlan(
        plan_id=id_factory.new_id("PLAN"),
        intent_id=intent.intent_id,
        risk_decision_id=decision.decision_id,
        correlation_id=intent.correlation_id,
        policy_version=decision.policy_version,
        orders=tuple(planned_orders),
        protective_orders=(),
        created_at=now,
        expires_at=decision.expires_at,
    )


class TestIronCondorPayoff:
    def test_equal_wing_formula_matches_generic(self) -> None:
        legs = [
            PayoffLeg(
                strike=Decimal("24000"),
                option_type=OptionType.PUT,
                side=Side.BUY,
                premium=Decimal("18"),
            ),
            PayoffLeg(
                strike=Decimal("24200"),
                option_type=OptionType.PUT,
                side=Side.SELL,
                premium=Decimal("35"),
            ),
            PayoffLeg(
                strike=Decimal("24800"),
                option_type=OptionType.CALL,
                side=Side.SELL,
                premium=Decimal("34"),
            ),
            PayoffLeg(
                strike=Decimal("25000"),
                option_type=OptionType.CALL,
                side=Side.BUY,
                premium=Decimal("17"),
            ),
        ]
        report = evaluate_same_expiry_payoff(
            legs,
            lot_size=65,
            structure_lots=1,
            family_id=FamilyId.short_iron_condor_defined,
        )
        assert report.status is PayoffStatus.IMPLEMENTED_UNIT
        assert report.formula_agrees is True

    def test_asymmetric_wing_formula_matches_generic(self) -> None:
        legs = [
            PayoffLeg(
                strike=Decimal("23800"),
                option_type=OptionType.PUT,
                side=Side.BUY,
                premium=Decimal("12"),
            ),
            PayoffLeg(
                strike=Decimal("24200"),
                option_type=OptionType.PUT,
                side=Side.SELL,
                premium=Decimal("35"),
            ),
            PayoffLeg(
                strike=Decimal("24800"),
                option_type=OptionType.CALL,
                side=Side.SELL,
                premium=Decimal("34"),
            ),
            PayoffLeg(
                strike=Decimal("25000"),
                option_type=OptionType.CALL,
                side=Side.BUY,
                premium=Decimal("17"),
            ),
        ]
        formula_loss, formula_profit = formula_short_iron_condor_defined(
            long_put_strike=Decimal("23800"),
            short_put_strike=Decimal("24200"),
            short_call_strike=Decimal("24800"),
            long_call_strike=Decimal("25000"),
            long_put_premium=Decimal("12"),
            short_put_premium=Decimal("35"),
            short_call_premium=Decimal("34"),
            long_call_premium=Decimal("17"),
            lot_size=65,
        )
        report = evaluate_same_expiry_payoff(
            legs,
            lot_size=65,
            family_id=FamilyId.short_iron_condor_defined,
        )
        assert report.formula_agrees is True
        assert report.generic_max_loss == formula_loss
        assert report.generic_max_profit == formula_profit


class TestIronCondorStrategyAndBinder:
    def test_strategy_stamps_m4_family_and_long_first_legs(self) -> None:
        candidates = _condor_candidates()
        ctx = StrategyContext(
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
        decision = IronCondorStrategy().evaluate(ctx)
        assert decision.emits_intent
        intent = decision.intents[0]
        assert intent.mode_id is ModeId.M4_STRATEGIC_POSITIONAL
        assert intent.family_id == FamilyId.short_iron_condor_defined.value
        assert intent.legs[0].side is Side.BUY
        assert intent.legs[1].side is Side.SELL
        assert intent.legs[2].side is Side.SELL
        assert intent.legs[3].side is Side.BUY

    def test_bind_iron_condor_selects_long_first_symbols_on_range(self) -> None:
        bound = bind_iron_condor(
            _condor_candidates(),
            market=_market(TrendState.RANGE),
            policy=IDENTIFICATION_POLICY,
        )
        assert bound.binding.eligible is True
        assert bound.binding.strategy_id == "short_iron_condor_defined"
        assert bound.binding.selected_symbols == (
            "NIFTY26OCT24400PE",
            "NIFTY26OCT24440PE",
            "NIFTY26OCT24560CE",
            "NIFTY26OCT24600CE",
        )
        assert bound.candidates[0].contract.strike == Decimal("24400")
        assert bound.candidates[1].contract.strike == Decimal("24440")

    def test_bind_iron_condor_abstains_on_directional_trend(self) -> None:
        bound = bind_iron_condor(
            _condor_candidates(),
            market=_market(TrendState.UP),
            policy=IDENTIFICATION_POLICY,
        )
        assert bound.binding.eligible is False


class TestIronCondorGatewayPrefixSafety:
    def test_gateway_approves_long_legs_before_short_legs(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        ids = SequentialIdFactory(clock.instant)
        reservation_service = CapitalReservationService(
            store, clock=clock, id_factory=ids
        )
        gateway = RiskGateway(
            risk_policy=RISK_POLICY,
            account_config=ACCOUNT_CONFIG,
            reservation_service=reservation_service,
            margin_preview=ConfirmedMarginPreview(),
            clock=clock,
            id_factory=ids,
            nifty_only_execution=True,
        )
        candidates = _condor_candidates()
        ctx = StrategyContext(
            underlying=f.snapshot(
                snapshot_id="SNAP-1",
                contract=f.index_contract(),
                market=f.quote(last=f.price("24500"), close=f.price("24500")),
            ),
            candidates=candidates,
            view=f.portfolio_view(),
            now=clock.instant,
            macro=_macro(),
        )
        intent = IronCondorStrategy().evaluate(ctx).intents[0]
        legs = condor_legs(intent)
        leg_snapshots = {
            legs.long_put.leg_id: candidates[0],
            legs.short_put.leg_id: candidates[1],
            legs.short_call.leg_id: candidates[2],
            legs.long_call.leg_id: candidates[3],
        }
        decision = gateway.evaluate(
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
                    "NIFTY26OCT24440PE", Decimal("24440"), OptionType.PUT
                ),
                leg_snapshots=leg_snapshots,
                event_risk_state=f.event_risk_state(),
            )
        )
        assert decision.action in {RiskAction.APPROVE, RiskAction.RESIZE}
        assert [leg.leg_id for leg in decision.approved_legs] == [
            "leg-long-put",
            "leg-short-put",
            "leg-long-call",
            "leg-short-call",
        ]


class TestIronCondorG2Lifecycle:
    def test_g2_lifecycle_entry_monitor_exit_restart(
        self, store: TradingStore, clock: FrozenClock
    ) -> None:
        ids = SequentialIdFactory(clock.instant)
        broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
        planner = OrderPlanPlanner(clock=clock, id_factory=ids)
        oms = OmsEngine(
            store,
            broker,
            clock=clock,
            id_factory=ids,
            rate_limiter=OrderRateLimiter(clock=clock),
        )
        reservation_service = CapitalReservationService(
            store, clock=clock, id_factory=ids
        )
        manager = TradeManager(
            clock=clock, id_factory=ids, reservation_service=reservation_service
        )
        gateway = RiskGateway(
            risk_policy=RISK_POLICY,
            account_config=ACCOUNT_CONFIG,
            reservation_service=reservation_service,
            margin_preview=ConfirmedMarginPreview(),
            clock=clock,
            id_factory=ids,
            nifty_only_execution=True,
        )

        candidates = _condor_candidates()
        ctx = StrategyContext(
            underlying=f.snapshot(
                snapshot_id="SNAP-1",
                contract=f.index_contract(),
                market=f.quote(last=f.price("24500"), close=f.price("24500")),
            ),
            candidates=candidates,
            view=f.portfolio_view(),
            now=clock.instant,
            macro=_macro(),
        )
        intent = IronCondorStrategy().evaluate(ctx).intents[0]
        legs = condor_legs(intent)
        leg_snapshots = {
            legs.long_put.leg_id: candidates[0],
            legs.short_put.leg_id: candidates[1],
            legs.short_call.leg_id: candidates[2],
            legs.long_call.leg_id: candidates[3],
        }
        risk_dec = gateway.evaluate(
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
                    "NIFTY26OCT24440PE", Decimal("24440"), OptionType.PUT
                ),
                leg_snapshots=leg_snapshots,
                event_risk_state=f.event_risk_state(),
            )
        )
        plan = planner.build(
            OrderPlanRequest(
                intent=intent,
                decision=risk_dec,
                feature_snapshot=candidates[0],
                account_id=ACCOUNT_ID,
                leg_snapshots=leg_snapshots,
            )
        )
        assert plan.orders[0].command.side is Side.BUY
        assert plan.orders[1].command.side is Side.SELL

        trade_id = manager.begin_entry(intent, plan)
        submit_res = oms.submit_plan(
            plan, strategy_id=intent.strategy_id, account_id=ACCOUNT_ID
        )
        for event in submit_res.events:
            manager.apply_order_event(
                event,
                intent=intent,
                capital_reservation_id=risk_dec.capital_reservation_id,
            )
        pos = manager.register_protective_orders(
            trade_id, tuple(stub.stub_id for stub in plan.protective_orders)
        )
        assert pos.state is TradeState.OPEN
        assert pos.mode_id is ModeId.M4_STRATEGIC_POSITIONAL

        rec = PositionLifecycleRecord(
            trade_id=trade_id,
            position=pos,
            intent=intent,
            risk_decision=risk_dec,
            holding_style=HoldingStyle.POSITIONAL,
            as_of=clock.instant,
            mode_id=pos.mode_id,
        )
        store.upsert_position_lifecycle(rec, event_id=ids.new_id("EVT"))
        restored = store.list_position_lifecycle()[0]
        manager_recovered = TradeManager(
            clock=clock, id_factory=ids, reservation_service=reservation_service
        )
        manager_recovered.restore_position(restored.position)
        assert (
            manager_recovered.get_position(trade_id).mode_id
            is ModeId.M4_STRATEGIC_POSITIONAL
        )

        stop_snapshot = _adverse_exit_snapshots(legs, candidates)
        evaluation = manager_recovered.evaluate_exit(
            trade_id,
            stop_snapshot[legs.short_put.leg_id],
            intent,
            leg_snapshots=stop_snapshot,
        )
        assert evaluation.should_exit
        manager_recovered.apply_exit_evaluation(trade_id, evaluation)
        exit_plan = _build_exit_plan(
            intent=intent,
            decision=risk_dec,
            entry_events=submit_res.events,
            leg_snapshots=stop_snapshot,
            account_id=ACCOUNT_ID,
            clock=clock,
            id_factory=ids,
        )
        exit_submit = oms.submit_plan(
            exit_plan, strategy_id=intent.strategy_id, account_id=ACCOUNT_ID
        )
        for event in exit_submit.events:
            pos = manager_recovered.apply_exit_order_event(
                event, capital_reservation_id=risk_dec.capital_reservation_id
            )
        assert pos.state is TradeState.CLOSED

    def test_partial_short_fill_routes_to_repair_required(
        self, clock: FrozenClock
    ) -> None:
        ids = SequentialIdFactory(clock.instant)
        manager = TradeManager(clock=clock, id_factory=ids)
        candidates = _condor_candidates()
        ctx = StrategyContext(
            underlying=f.snapshot(contract=f.index_contract()),
            candidates=candidates,
            view=f.portfolio_view(),
            now=clock.instant,
            macro=_macro(),
        )
        intent = IronCondorStrategy().evaluate(ctx).intents[0]
        decision = f.risk_decision(
            intent_id=intent.intent_id,
            approved_legs=tuple(f.approved_leg(leg.leg_id) for leg in intent.legs),
        )
        legs = condor_legs(intent)
        leg_snapshots = {
            legs.long_put.leg_id: candidates[0],
            legs.short_put.leg_id: candidates[1],
            legs.short_call.leg_id: candidates[2],
            legs.long_call.leg_id: candidates[3],
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
            order for order in plan.orders if order.leg_id == "leg-short-put"
        )
        partial = f.order_event(
            state=OrderState.PARTIAL,
            filled_quantity=20,
            average_fill_price=short_order.command.limit_price,
            identity=f.order_identity(trade_id=trade_id, broker_order_id="BRK-IC-PART"),
            command=short_order.command,
        )
        repaired = manager.apply_order_event(partial, intent=intent)
        assert repaired.state is TradeState.REPAIR_REQUIRED
        assert repaired.state is not TradeState.OPEN


class TestIronCondorStartupValidation:
    def test_short_iron_condor_defined_may_run_paper_after_g2(self) -> None:
        cfg = PaperSessionConfig.model_validate(
            {
                "poll_interval_seconds": 60,
                "eod_local": "15:40",
                "option_strikes_each_side": 2,
                "experiment_prefix": "EXP-TEST",
                "strategy_ids": ("short_iron_condor_defined",),
                "strategy_stances": {
                    "short_iron_condor_defined": ExecutionMode.PAPER,
                },
                "commodity_underlying": "CRUDEOIL",
                "commodity_exchange": "MCX",
                "commodity_segment": "MCX_COM",
                "cohort_dir": "data/paper/cohorts",
                "store_path": "data/paper/trading.sqlite",
                "broker_state_path": "data/paper/broker_state.json",
            }
        )
        validated, warnings = validate_startup_configuration(cfg)
        assert (
            validated.strategy_stances["short_iron_condor_defined"]
            is ExecutionMode.PAPER
        )
        assert warnings == []

    def test_legacy_iron_condor_strategy_id_still_blocked(self) -> None:
        cfg = PaperSessionConfig.model_validate(
            {
                "poll_interval_seconds": 60,
                "eod_local": "15:40",
                "option_strikes_each_side": 2,
                "experiment_prefix": "EXP-TEST",
                "strategy_ids": ("iron_condor",),
                "strategy_stances": {"iron_condor": ExecutionMode.PAPER},
                "commodity_underlying": "CRUDEOIL",
                "commodity_exchange": "MCX",
                "commodity_segment": "MCX_COM",
                "cohort_dir": "data/paper/cohorts",
                "store_path": "data/paper/trading.sqlite",
                "broker_state_path": "data/paper/broker_state.json",
            }
        )
        with pytest.raises(
            StartupValidationError, match="Gate G2 violation.*iron_condor"
        ):
            validate_startup_configuration(cfg)
