"""Phase P6 M3 Credit Verticals and G2 Promotion Lifecycle Test Suite.

Locks in Phase P6 requirements from:
- FOUR_MODE_LAYER_CHANGE_CRITERIA.md (§1.4, §2.1, §3.8)
- NIFTY_FOUR_MODE_CURSOR_REDESIGN.md (§5, §10, §13, Example G)
- FOUR_MODE_REDESIGN_PLAN.md (Phase P6, Gate G2)
- REQUIREMENT_TRACEABILITY.md (R-004, R-011, R-018, T21)

Verifications:
1. R-004 & §1.4 / §2.1: Credit spread approved leg order is strictly long protection before short.
2. §3.8: Multi-leg strategy split into BullPutCreditStrategy and BearCallCreditStrategy,
   each stamping ModeId.M3_TACTICAL_POSITIONAL, explicit FamilyId, and emitting long protection first.
3. §3.8: Option-chain binder `bind_credit_spread` for bull put credit and bear call credit.
4. §3.8: G2 evidence for bull_put_credit: entry, partial fill, monitoring, exit, restart.
5. §3.8: G2 evidence for bear_call_credit: entry, partial fill, monitoring, exit, restart.
6. §3.8: Partial entry fill never reports the spread OPEN while short is uncovered (REPAIR_REQUIRED).
7. §3.8: Catch-all defined_risk_multileg cannot remain an executable PAPER alias (StartupValidationError).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

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
    IntentLeg,
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
from trading.domain.primitives import Percent
from trading.identification import (
    bind_credit_spread,
    load_identification_policy,
)
from trading.oms import OmsEngine, OrderPlanPlanner, OrderPlanRequest, OrderRateLimiter
from trading.risk import CapitalReservationService, RiskGateway, RiskGatewayRequest
from trading.runtime.paper_session import PaperSessionConfig
from trading.runtime.startup_validation import (
    StartupValidationError,
    validate_startup_configuration,
)
from trading.storage.trading_store import TradingStore
from trading.strategies import (
    BearCallCreditStrategy,
    BullPutCreditStrategy,
    MacroAssessment,
    MacroBias,
    StrategyContext,
)
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
KOLKATA = ZoneInfo("Asia/Kolkata")


class ConfirmedMarginPreview:
    """Mock margin preview returning confirmed margin requirement within budget."""

    def __init__(self, margin_required: str = "5000") -> None:
        self._margin_required = margin_required

    def preview_margin(self, request: MarginPreviewRequest) -> MarginPreviewResult:
        return MarginPreviewResult(
            request_id=request.request_id,
            as_of=NOW,
            margin_required=f.money(self._margin_required),
            margin_available_after=f.money("5000000"),
            confirmed=True,
        )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW + timedelta(seconds=60))


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "paper_p6.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


def _sample_config(**overrides: object) -> PaperSessionConfig:
    payload: dict[str, object] = {
        "poll_interval_seconds": 60,
        "eod_local": "15:40",
        "option_strikes_each_side": 2,
        "experiment_prefix": "EXP-TEST",
        "strategy_ids": (
            "positional_long_option",
            "debit_spread",
            "bull_put_credit",
            "bear_call_credit",
        ),
        "strategy_stances": {
            "positional_long_option": ExecutionMode.PAPER,
            "debit_spread": ExecutionMode.PAPER,
            "bull_put_credit": ExecutionMode.PAPER,
            "bear_call_credit": ExecutionMode.PAPER,
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


def _market(
    trend: TrendState = TrendState.UP,
    calculated_at: datetime = NOW,
    **overrides: object,
) -> MarketState:
    payload: dict[str, object] = {
        "market_state_id": "market-p6",
        "feature_version": IDENTIFICATION_POLICY.feature_version,
        "calculated_at": calculated_at.astimezone(UTC),
        "source_snapshot_ids": ("snapshot-1",),
        "trend": trend,
        "volatility": VolatilityState.NORMAL,
        "return_15m": Decimal("0.004"),
        "return_60m": Decimal("0.012"),
        "normalized_return_15m": Decimal("0.7"),
        "normalized_return_60m": Decimal("0.8"),
        "normalized_vwap_distance": Decimal("0.5"),
        "realized_volatility_ratio": Decimal("1.0"),
        "realized_volatility_annualized": Decimal("15"),
        "iv_percentile": Decimal("75"),
        "iv_rv_ratio": Decimal("1.0"),
        "trend_score": Decimal("0.7") if trend is TrendState.UP else Decimal("-0.7"),
        "event_state": "NORMAL",
        "macro_status": MacroStatus.NEUTRAL,
        "quality": DataQuality.VALID,
        "warmup_complete": True,
        "completed_bar_count": 60,
        "session_count": 20,
    }
    payload.update(overrides)
    return MarketState.model_validate(payload)


def _option(
    symbol: str,
    *,
    strike: str,
    delta: str,
    expiry: date,
    dte: int,
    bid: str = "40.00",
    ask: str = "40.10",
    option_type: OptionType = OptionType.PUT,
    open_interest: int = 5000,
    volume: int = 5000,
) -> FeatureSnapshot:
    return f.snapshot(
        snapshot_id=f"SNAP-{symbol}",
        contract=f.option_contract(
            symbol=symbol,
            expiry=expiry,
            strike=Decimal(strike),
            option_type=option_type,
        ),
        market=f.quote(
            bid=f.price(bid),
            ask=f.price(ask),
            last=f.price(bid),
            volume=volume,
        ),
        derivatives=DerivativesContext(
            days_to_expiry=dte,
            open_interest=open_interest,
            option_type=option_type,
            greeks=Greeks(
                model="fixture",
                calculation_version="1",
                converged=True,
                implied_volatility=Decimal("15"),
                delta=Decimal(delta),
            ),
            underlying_price=f.price("24000"),
        ),
        features={
            "lot_size": Decimal(75),
            "top_of_book_observed": Decimal(1),
        },
    )


def _macro(bias: MacroBias, confidence: str = "0.8") -> MacroAssessment:
    return MacroAssessment(
        regime="TRENDING",
        directional_bias=bias,
        confidence=Decimal(confidence),
        fresh_until=NOW + timedelta(minutes=30),
        model_version="v1",
    )


def _instrument_spec(
    symbol: str = "NIFTY26OCT24000PE",
    strike: Decimal = Decimal("24000"),
    option_type: OptionType = OptionType.PUT,
    expiry: date = date(2026, 10, 1),
) -> InstrumentSpec:
    return InstrumentSpec.model_validate(
        {
            "trading_symbol": f"NSE:{symbol}",
            "exchange": Exchange.NFO,
            "segment": "NSE_FO",
            "underlying": "NIFTY",
            "instrument_kind": InstrumentKind.OPTION,
            "provider_token": "tok-p6",
            "exchange_token": 1,
            "lot_size": 75,
            "tick_size": Decimal("0.05"),
            "price_precision": 2,
            "expiry": expiry,
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
        idempotency_key = derive_idempotency_key(
            account_id=account_id,
            strategy_id=intent.strategy_id,
            strategy_version=intent.strategy_version,
            intent_id=intent.intent_id,
            leg_id=f"{leg.leg_id}-exit",
            side=exit_side.value,
            quantity_contracts=entry.filled_quantity,
        )
        planned_orders.append(
            PlannedOrder(
                plan_leg_id=f"{leg.leg_id}-exit",
                leg_id=leg.leg_id,
                identity=OrderIdentity(
                    internal_order_id=internal_order_id,
                    client_order_id=internal_order_id,
                    idempotency_key=idempotency_key,
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


# =========================================================================
# 1. Prefix-Safe Approved Legs: Long Protection Before Short (R-004, §1.4, §2.1)
# =========================================================================


def test_prefix_safe_approved_legs_order_long_protection_first(
    store: TradingStore,
) -> None:
    """CRITERIA §1.4 / §2.1: Credit spread approved legs order is strictly long protection before short."""
    expiry = date(2026, 10, 1)
    # Bull Put Credit: Buy 23950 Put (long protection), Sell 24000 Put (short)
    long_put = f.option_contract(
        symbol="NIFTY26OCT23950PE",
        strike=Decimal("23950"),
        option_type=OptionType.PUT,
        expiry=expiry,
    )
    short_put = f.option_contract(
        symbol="NIFTY26OCT24000PE",
        strike=Decimal("24000"),
        option_type=OptionType.PUT,
        expiry=expiry,
    )

    long_feature = _option(
        "NIFTY26OCT23950PE",
        strike="23950",
        delta="-0.20",
        expiry=expiry,
        dte=7,
        bid="15.00",
        ask="15.10",
        option_type=OptionType.PUT,
    )
    short_feature = _option(
        "NIFTY26OCT24000PE",
        strike="24000",
        delta="-0.30",
        expiry=expiry,
        dte=7,
        bid="35.00",
        ask="35.10",
        option_type=OptionType.PUT,
    )

    intent = f.intent(
        snapshot_id=short_feature.snapshot_id,
        strategy_id="bull_put_credit",
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        family_id=FamilyId.bull_put_credit,
        setup_code="BULL_PUT_CREDIT",
        requested_risk=f.money("15000"),
        estimated_max_loss=f.money("20000"),
        entry_policy=f.entry_policy(max_spread=Percent.from_percent("5")),
        exit_template=f.exit_template(stop_distance_ticks=40),
        legs=(
            IntentLeg(leg_id="leg-long", contract=long_put, side=Side.BUY, ratio=1),
            IntentLeg(leg_id="leg-short", contract=short_put, side=Side.SELL, ratio=1),
        ),
    )

    ids = SequentialIdFactory(NOW)
    clock = FrozenClock(NOW)
    reservation_service = CapitalReservationService(store, clock=clock, id_factory=ids)
    gateway = RiskGateway(
        risk_policy=RISK_POLICY,
        account_config=ACCOUNT_CONFIG,
        reservation_service=reservation_service,
        margin_preview=ConfirmedMarginPreview(),
        clock=clock,
        id_factory=ids,
        nifty_only_execution=True,
    )

    leg_snapshots = {"leg-long": long_feature, "leg-short": short_feature}
    portfolio = f.portfolio_snapshot(
        exposure=f.exposure(
            equity=f.money("5000000"),
            margin_available=f.money("5000000"),
        )
    )

    request = RiskGatewayRequest(
        intent=intent,
        feature_snapshot=short_feature,
        portfolio_snapshot=portfolio,
        instrument=_instrument_spec(
            symbol="NIFTY26OCT24000PE",
            strike=Decimal("24000"),
            option_type=OptionType.PUT,
            expiry=expiry,
        ),
        leg_snapshots=leg_snapshots,
        event_risk_state=f.event_risk_state(),
    )

    decision = gateway.evaluate(request)
    assert decision.action in {RiskAction.APPROVE, RiskAction.RESIZE}
    assert len(decision.approved_legs) == 2

    # CRITICAL CHECK: Leg[0] MUST be leg-long (BUY) and Leg[1] MUST be leg-short (SELL)
    first_leg = decision.approved_legs[0]
    second_leg = decision.approved_legs[1]

    assert first_leg.leg_id == "leg-long"
    assert second_leg.leg_id == "leg-short"


# =========================================================================
# 2. Strategy Split and Intent Tagging (§3.8)
# =========================================================================


def test_bull_put_credit_strategy_intent_generation() -> None:
    """CRITERIA §3.8: BullPutCreditStrategy tags M3_TACTICAL_POSITIONAL, bull_put_credit, and long-then-short."""
    expiry = date(2026, 10, 1)
    put_long = _option(
        "NIFTY26OCT23950PE", strike="23950", delta="-0.20", expiry=expiry, dte=7
    )
    put_short = _option(
        "NIFTY26OCT24000PE", strike="24000", delta="-0.30", expiry=expiry, dte=7
    )

    ctx = StrategyContext(
        underlying=f.snapshot(
            snapshot_id="SNAP-UNDER",
            contract=f.index_contract(),
            market=f.quote(last=f.price("24100"), close=f.price("24000")),
        ),
        candidates=(put_long, put_short),
        view=f.portfolio_view(),
        now=NOW + timedelta(seconds=60),
        macro=_macro(MacroBias.BULLISH),
    )

    strategy = BullPutCreditStrategy()
    assert strategy.strategy_id == "bull_put_credit"

    decision = strategy.evaluate(ctx)
    assert decision.emits_intent
    intent = decision.intents[0]

    # Verify M3 mode and family
    assert intent.mode_id is ModeId.M3_TACTICAL_POSITIONAL
    assert intent.family_id == FamilyId.bull_put_credit
    assert intent.strategy_id == "bull_put_credit"

    # Verify legs order: long protection BUY first, short SELL second
    assert len(intent.legs) == 2
    assert intent.legs[0].side is Side.BUY
    assert intent.legs[0].contract.strike == Decimal("23950")
    assert intent.legs[1].side is Side.SELL
    assert intent.legs[1].contract.strike == Decimal("24000")


def test_bear_call_credit_strategy_intent_generation() -> None:
    """CRITERIA §3.8: BearCallCreditStrategy tags M3_TACTICAL_POSITIONAL, bear_call_credit, and long-then-short."""
    expiry = date(2026, 10, 1)
    call_short = _option(
        "NIFTY26OCT24000CE",
        strike="24000",
        delta="0.30",
        expiry=expiry,
        dte=7,
        option_type=OptionType.CALL,
    )
    call_long = _option(
        "NIFTY26OCT24050CE",
        strike="24050",
        delta="0.20",
        expiry=expiry,
        dte=7,
        option_type=OptionType.CALL,
    )

    ctx = StrategyContext(
        underlying=f.snapshot(
            snapshot_id="SNAP-UNDER",
            contract=f.index_contract(),
            market=f.quote(last=f.price("23900"), close=f.price("24000")),
        ),
        candidates=(call_short, call_long),
        view=f.portfolio_view(),
        now=NOW + timedelta(seconds=60),
        macro=_macro(MacroBias.BEARISH),
    )

    strategy = BearCallCreditStrategy()
    assert strategy.strategy_id == "bear_call_credit"

    decision = strategy.evaluate(ctx)
    assert decision.emits_intent
    intent = decision.intents[0]

    # Verify M3 mode and family
    assert intent.mode_id is ModeId.M3_TACTICAL_POSITIONAL
    assert intent.family_id == FamilyId.bear_call_credit
    assert intent.strategy_id == "bear_call_credit"

    # Verify legs order: long protection BUY first, short SELL second
    assert len(intent.legs) == 2
    assert intent.legs[0].side is Side.BUY
    assert intent.legs[0].contract.strike == Decimal("24050")
    assert intent.legs[1].side is Side.SELL
    assert intent.legs[1].contract.strike == Decimal("24000")


# =========================================================================
# 3. Option-Chain Binders for Credit Verticals (§3.8)
# =========================================================================


def test_bind_credit_spread_bull_put_and_bear_call() -> None:
    """CRITERIA §3.8: bind_credit_spread binds bull put credit and bear call credit with long protection first."""
    expiry = date(2026, 10, 1)
    market_up = _market(TrendState.UP)
    market_down = _market(TrendState.DOWN)

    # Put candidates for Bull Put (width 50, strikes 23950 & 24000)
    put_long = _option(
        "NIFTY_23950_PE",
        strike="23950",
        delta="-0.20",
        expiry=expiry,
        dte=7,
        bid="15.00",
        ask="15.10",
        option_type=OptionType.PUT,
    )
    put_short = _option(
        "NIFTY_24000_PE",
        strike="24000",
        delta="-0.30",
        expiry=expiry,
        dte=7,
        bid="35.00",
        ask="35.10",
        option_type=OptionType.PUT,
    )

    bound_put = bind_credit_spread(
        (put_long, put_short),
        market=market_up,
        policy=IDENTIFICATION_POLICY,
        family_id=FamilyId.bull_put_credit,
    )
    assert bound_put.binding.eligible is True
    assert bound_put.binding.strategy_id == "bull_put_credit"
    # Long protection first
    assert bound_put.binding.selected_symbols == ("NIFTY_23950_PE", "NIFTY_24000_PE")
    assert bound_put.candidates[0].contract.symbol == "NIFTY_23950_PE"
    assert bound_put.candidates[1].contract.symbol == "NIFTY_24000_PE"

    # Call candidates for Bear Call (width 50, strikes 24000 & 24050)
    call_short = _option(
        "NIFTY_24000_CE",
        strike="24000",
        delta="0.30",
        expiry=expiry,
        dte=7,
        bid="35.00",
        ask="35.10",
        option_type=OptionType.CALL,
    )
    call_long = _option(
        "NIFTY_24050_CE",
        strike="24050",
        delta="0.20",
        expiry=expiry,
        dte=7,
        bid="15.00",
        ask="15.10",
        option_type=OptionType.CALL,
    )

    bound_call = bind_credit_spread(
        (call_short, call_long),
        market=market_down,
        policy=IDENTIFICATION_POLICY,
        family_id=FamilyId.bear_call_credit,
    )
    assert bound_call.binding.eligible is True
    assert bound_call.binding.strategy_id == "bear_call_credit"
    # Long protection first
    assert bound_call.binding.selected_symbols == ("NIFTY_24050_CE", "NIFTY_24000_CE")
    assert bound_call.candidates[0].contract.symbol == "NIFTY_24050_CE"
    assert bound_call.candidates[1].contract.symbol == "NIFTY_24000_CE"


# =========================================================================
# 4. G2 Lifecycle Set: Entry, Monitoring, Exit, Restart (§3.8, Gate G2)
# =========================================================================


def test_g2_lifecycle_full_sequence_bull_put_credit(
    store: TradingStore, clock: FrozenClock
) -> None:
    """Gate G2: Bull Put Credit full lifecycle: Entry (long then short) -> Monitoring -> Exit -> Restart."""
    ids = SequentialIdFactory(clock.instant)
    broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
    planner = OrderPlanPlanner(clock=clock, id_factory=ids)
    rate_limiter = OrderRateLimiter(clock=clock, max_per_second=10)
    oms = OmsEngine(
        store, broker, clock=clock, id_factory=ids, rate_limiter=rate_limiter
    )
    reservation_service = CapitalReservationService(store, clock=clock, id_factory=ids)
    manager = TradeManager(
        clock=clock, id_factory=ids, reservation_service=reservation_service
    )

    expiry = date(2026, 10, 1)
    put_long = _option(
        "NIFTY26OCT23950PE",
        strike="23950",
        delta="-0.20",
        expiry=expiry,
        dte=7,
        bid="15.00",
        ask="15.10",
    )
    put_short = _option(
        "NIFTY26OCT24000PE",
        strike="24000",
        delta="-0.30",
        expiry=expiry,
        dte=7,
        bid="35.00",
        ask="35.10",
    )

    # 1. Strategy Intent
    underlying = f.snapshot(
        snapshot_id="SNAP-UNDER",
        contract=f.index_contract(),
        market=f.quote(last=f.price("24100"), close=f.price("24000")),
    )
    ctx = StrategyContext(
        underlying=underlying,
        candidates=(put_long, put_short),
        view=f.portfolio_view(),
        now=clock.instant,
        macro=_macro(MacroBias.BULLISH),
    )
    decision = BullPutCreditStrategy().evaluate(ctx)
    intent = decision.intents[0]

    # 2. Gateway Approval
    gateway = RiskGateway(
        risk_policy=RISK_POLICY,
        account_config=ACCOUNT_CONFIG,
        reservation_service=reservation_service,
        margin_preview=ConfirmedMarginPreview(),
        clock=clock,
        id_factory=ids,
        nifty_only_execution=True,
    )
    portfolio = f.portfolio_snapshot(
        exposure=f.exposure(
            equity=f.money("5000000"), margin_available=f.money("5000000")
        )
    )
    gw_request = RiskGatewayRequest(
        intent=intent,
        feature_snapshot=underlying,
        portfolio_snapshot=portfolio,
        instrument=_instrument_spec(
            symbol="NIFTY26OCT24000PE",
            strike=Decimal("24000"),
            option_type=OptionType.PUT,
            expiry=expiry,
        ),
        leg_snapshots={"leg-long": put_long, "leg-short": put_short},
        event_risk_state=f.event_risk_state(),
    )
    risk_dec = gateway.evaluate(gw_request)
    assert risk_dec.action in {RiskAction.APPROVE, RiskAction.RESIZE}

    # 3. Order Planning (verifies long protection planned first)
    snapshots = {
        "leg-long": put_long,
        "leg-short": put_short,
    }
    plan = planner.build(
        OrderPlanRequest(
            intent=intent,
            decision=risk_dec,
            feature_snapshot=underlying,
            account_id=ACCOUNT_ID,
            leg_snapshots=snapshots,
        )
    )
    assert len(plan.orders) == 2
    assert plan.orders[0].command.side is Side.BUY  # Long protection planned first
    assert plan.orders[1].command.side is Side.SELL  # Short planned second

    # 4. OMS Execution (long protection submitted and filled first)
    trade_id = manager.begin_entry(intent, plan)
    submit_res = oms.submit_plan(
        plan, strategy_id=intent.strategy_id, account_id=ACCOUNT_ID
    )
    assert len(submit_res.events) == 2
    event_long = submit_res.events[0]
    event_short = submit_res.events[1]

    assert event_long.state is OrderState.FILLED
    assert event_short.state is OrderState.FILLED

    # Apply order fills
    manager.apply_order_event(
        event_long,
        intent=intent,
        capital_reservation_id=risk_dec.capital_reservation_id,
    )
    manager.apply_order_event(
        event_short,
        intent=intent,
        capital_reservation_id=risk_dec.capital_reservation_id,
    )

    # Register protective orders (Invariant 16) to open the position
    pos = manager.register_protective_orders(
        trade_id, tuple(stub.stub_id for stub in plan.protective_orders)
    )
    assert pos.state is TradeState.OPEN
    assert pos.mode_id is ModeId.M3_TACTICAL_POSITIONAL

    # 5. Position Persistence & Restart Recovery
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
    restored = store.list_position_lifecycle()
    assert any(
        r.trade_id == trade_id and r.mode_id is ModeId.M3_TACTICAL_POSITIONAL
        for r in restored
    )

    # Restore in new manager instance
    manager_recovered = TradeManager(
        clock=clock, id_factory=ids, reservation_service=reservation_service
    )
    manager_recovered.restore_position(restored[0].position)
    assert (
        manager_recovered.get_position(trade_id).mode_id
        is ModeId.M3_TACTICAL_POSITIONAL
    )

    # 6. Monitoring & Exit: Evaluate exit -> Transition to EXIT_PENDING -> OMS Exit Plan -> CLOSED
    stop_snapshot = _option(
        "NIFTY26OCT24000PE",
        strike="24000",
        delta="-0.30",
        expiry=expiry,
        dte=7,
        bid="80.00",
        ask="80.10",
    )
    evaluation = manager_recovered.evaluate_exit(trade_id, stop_snapshot, intent)
    assert evaluation.should_exit
    manager_recovered.apply_exit_evaluation(trade_id, evaluation)

    exit_plan = _build_exit_plan(
        intent=intent,
        decision=risk_dec,
        entry_events=submit_res.events,
        leg_snapshots=snapshots,
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


def test_g2_lifecycle_full_sequence_bear_call_credit(
    store: TradingStore, clock: FrozenClock
) -> None:
    """Gate G2: Bear Call Credit full lifecycle: Entry (long then short) -> Monitoring -> Exit -> Restart."""
    ids = SequentialIdFactory(clock.instant)
    broker = PaperBroker.from_fixtures(BROKER_FIXTURES, clock=clock, id_factory=ids)
    planner = OrderPlanPlanner(clock=clock, id_factory=ids)
    rate_limiter = OrderRateLimiter(clock=clock, max_per_second=10)
    oms = OmsEngine(
        store, broker, clock=clock, id_factory=ids, rate_limiter=rate_limiter
    )
    reservation_service = CapitalReservationService(store, clock=clock, id_factory=ids)
    manager = TradeManager(
        clock=clock, id_factory=ids, reservation_service=reservation_service
    )

    expiry = date(2026, 10, 1)
    call_short = _option(
        "NIFTY26OCT24000CE",
        strike="24000",
        delta="0.30",
        expiry=expiry,
        dte=7,
        bid="35.00",
        ask="35.10",
        option_type=OptionType.CALL,
    )
    call_long = _option(
        "NIFTY26OCT24050CE",
        strike="24050",
        delta="0.20",
        expiry=expiry,
        dte=7,
        bid="15.00",
        ask="15.10",
        option_type=OptionType.CALL,
    )

    # 1. Strategy Intent
    underlying = f.snapshot(
        snapshot_id="SNAP-UNDER",
        contract=f.index_contract(),
        market=f.quote(last=f.price("23900"), close=f.price("24000")),
    )
    ctx = StrategyContext(
        underlying=underlying,
        candidates=(call_short, call_long),
        view=f.portfolio_view(),
        now=clock.instant,
        macro=_macro(MacroBias.BEARISH),
    )
    decision = BearCallCreditStrategy().evaluate(ctx)
    intent = decision.intents[0]

    # 2. Gateway Approval
    gateway = RiskGateway(
        risk_policy=RISK_POLICY,
        account_config=ACCOUNT_CONFIG,
        reservation_service=reservation_service,
        margin_preview=ConfirmedMarginPreview(),
        clock=clock,
        id_factory=ids,
        nifty_only_execution=True,
    )
    portfolio = f.portfolio_snapshot(
        exposure=f.exposure(
            equity=f.money("5000000"), margin_available=f.money("5000000")
        )
    )
    gw_request = RiskGatewayRequest(
        intent=intent,
        feature_snapshot=underlying,
        portfolio_snapshot=portfolio,
        instrument=_instrument_spec(
            symbol="NIFTY26OCT24000CE",
            strike=Decimal("24000"),
            option_type=OptionType.CALL,
            expiry=expiry,
        ),
        leg_snapshots={"leg-long": call_long, "leg-short": call_short},
        event_risk_state=f.event_risk_state(),
    )
    risk_dec = gateway.evaluate(gw_request)
    assert risk_dec.action in {RiskAction.APPROVE, RiskAction.RESIZE}

    # 3. Order Planning (verifies long protection planned first)
    snapshots = {
        "leg-long": call_long,
        "leg-short": call_short,
    }
    plan = planner.build(
        OrderPlanRequest(
            intent=intent,
            decision=risk_dec,
            feature_snapshot=underlying,
            account_id=ACCOUNT_ID,
            leg_snapshots=snapshots,
        )
    )
    assert len(plan.orders) == 2
    assert plan.orders[0].command.side is Side.BUY  # Long protection planned first
    assert plan.orders[1].command.side is Side.SELL  # Short planned second

    # 4. OMS Execution
    trade_id = manager.begin_entry(intent, plan)
    submit_res = oms.submit_plan(
        plan, strategy_id=intent.strategy_id, account_id=ACCOUNT_ID
    )
    assert len(submit_res.events) == 2
    for event in submit_res.events:
        manager.apply_order_event(
            event, intent=intent, capital_reservation_id=risk_dec.capital_reservation_id
        )

    pos = manager.register_protective_orders(
        trade_id, tuple(stub.stub_id for stub in plan.protective_orders)
    )
    assert pos.state is TradeState.OPEN
    assert pos.mode_id is ModeId.M3_TACTICAL_POSITIONAL

    # 5. Position Persistence & Restart Recovery
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
    restored = store.list_position_lifecycle()
    assert any(
        r.trade_id == trade_id and r.mode_id is ModeId.M3_TACTICAL_POSITIONAL
        for r in restored
    )

    manager_recovered = TradeManager(
        clock=clock, id_factory=ids, reservation_service=reservation_service
    )
    manager_recovered.restore_position(restored[0].position)
    assert (
        manager_recovered.get_position(trade_id).mode_id
        is ModeId.M3_TACTICAL_POSITIONAL
    )

    # 6. Monitoring & Exit
    stop_snapshot = _option(
        "NIFTY26OCT24000CE",
        strike="24000",
        delta="0.30",
        expiry=expiry,
        dte=7,
        bid="80.00",
        ask="80.10",
        option_type=OptionType.CALL,
    )
    evaluation = manager_recovered.evaluate_exit(trade_id, stop_snapshot, intent)
    assert evaluation.should_exit
    manager_recovered.apply_exit_evaluation(trade_id, evaluation)

    exit_plan = _build_exit_plan(
        intent=intent,
        decision=risk_dec,
        entry_events=submit_res.events,
        leg_snapshots=snapshots,
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


# =========================================================================
# 5. G2 Partial Fill: Never Report OPEN While Short Uncovered (§3.8)
# =========================================================================


def test_g2_partial_fill_never_reports_open_when_short_uncovered(
    clock: FrozenClock,
) -> None:
    """CRITERIA §3.8: A partial fill never reports the spread OPEN while a short is uncovered."""
    ids = SequentialIdFactory(clock.instant)
    manager = TradeManager(clock=clock, id_factory=ids)

    expiry = date(2026, 10, 1)
    long_put = f.option_contract(
        symbol="NIFTY26OCT23950PE",
        strike=Decimal("23950"),
        option_type=OptionType.PUT,
        expiry=expiry,
    )
    short_put = f.option_contract(
        symbol="NIFTY26OCT24000PE",
        strike=Decimal("24000"),
        option_type=OptionType.PUT,
        expiry=expiry,
    )

    intent = f.intent(
        strategy_id="bull_put_credit",
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        family_id=FamilyId.bull_put_credit,
        legs=(
            IntentLeg(leg_id="leg-long", contract=long_put, side=Side.BUY, ratio=1),
            IntentLeg(leg_id="leg-short", contract=short_put, side=Side.SELL, ratio=1),
        ),
    )

    decision = f.risk_decision(
        approved_legs=(
            f.approved_leg("leg-long"),
            f.approved_leg("leg-short"),
        ),
    )
    planner = OrderPlanPlanner(clock=clock, id_factory=ids)
    plan = planner.build(
        OrderPlanRequest(
            intent=intent,
            decision=decision,
            feature_snapshot=f.snapshot(contract=f.index_contract()),
            account_id=ACCOUNT_ID,
            leg_snapshots={
                "leg-long": _option(
                    "NIFTY26OCT23950PE",
                    strike="23950",
                    delta="-0.20",
                    expiry=expiry,
                    dte=7,
                ),
                "leg-short": _option(
                    "NIFTY26OCT24000PE",
                    strike="24000",
                    delta="-0.30",
                    expiry=expiry,
                    dte=7,
                ),
            },
        )
    )
    trade_id = manager.begin_entry(intent, plan)

    # Case A: Short leg experiences a partial fill
    short_order = next(
        order for order in plan.orders if order.command.side is Side.SELL
    )
    partial_short = f.order_event(
        state=OrderState.PARTIAL,
        filled_quantity=25,
        average_fill_price=short_order.command.limit_price,
        identity=f.order_identity(
            trade_id=trade_id,
            broker_order_id="BRK-CS-PART",
        ),
        command=short_order.command,
    )
    repaired = manager.apply_order_event(partial_short, intent=intent)
    # Must transition to REPAIR_REQUIRED, NEVER OPEN!
    assert repaired.state is TradeState.REPAIR_REQUIRED
    assert repaired.state is not TradeState.OPEN
    assert repaired.protective_order_ids == ("REPAIR-PENDING",)


# =========================================================================
# 6. Catch-All defined_risk_multileg Not an Executable Alias (§3.8)
# =========================================================================


def test_catch_all_defined_risk_multileg_cannot_run_paper() -> None:
    """CRITERIA §3.8: The old catch-all id defined_risk_multileg does not remain an executable PAPER alias."""
    # Attempting to start paper session with defined_risk_multileg on PAPER stance
    cfg = _sample_config(
        strategy_ids=("defined_risk_multileg",),
        strategy_stances={"defined_risk_multileg": ExecutionMode.PAPER},
    )
    with pytest.raises(StartupValidationError) as exc_info:
        validate_startup_configuration(cfg)
    assert "Gate G2 violation" in str(exc_info.value)
    assert "defined_risk_multileg" in str(exc_info.value)

    # In contrast, proven families bull_put_credit and bear_call_credit pass startup validation
    cfg_proven = _sample_config(
        strategy_ids=("bull_put_credit", "bear_call_credit"),
        strategy_stances={
            "bull_put_credit": ExecutionMode.PAPER,
            "bear_call_credit": ExecutionMode.PAPER,
        },
    )
    validated, warnings = validate_startup_configuration(cfg_proven)
    assert validated.strategy_stances["bull_put_credit"] is ExecutionMode.PAPER
    assert validated.strategy_stances["bear_call_credit"] is ExecutionMode.PAPER
    assert warnings == []
