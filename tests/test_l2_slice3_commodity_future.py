"""E2E vertical slice 3: commodity futures paper path (L2-012)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import tests.factories as f
from trading.broker.paper import PaperBroker
from trading.config import load_config, load_risk_policy
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    DerivativesContext,
    FeatureSnapshot,
    InstrumentSpec,
    IntentLeg,
    OrderCommand,
    OrderEvent,
    OrderIdentity,
    OrderPlan,
    PlannedOrder,
    RiskDecision,
    TradeIntent,
)
from trading.domain.enums import (
    AssetClass,
    Exchange,
    InstrumentKind,
    OrderPlanState,
    OrderState,
    OrderType,
    ReadinessLevel,
    ReasonCode,
    RiskAction,
    Side,
    TimeInForce,
)
from trading.domain.ids import SequentialIdFactory, derive_idempotency_key
from trading.domain.primitives import Price, TickSize
from trading.oms import OmsEngine, OrderPlanPlanner, OrderPlanRequest, OrderRateLimiter
from trading.portfolio import PortfolioReconciler, build_broker_snapshot
from trading.risk import CapitalReservationService, RiskGateway, RiskGatewayRequest
from trading.safety import ReadinessEvaluator, ReadinessRequest, SafetyControls
from trading.storage.trading_store import TradingStore
from trading.trade import ExitKind, TradeManager

ROOT = Path(__file__).resolve().parent.parent
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
ACCOUNT_ID = "ACC-PAPER-1"
STRATEGY_ID = "positional_index_options_poc"
NOW = f.NOW
COMMODITY_TICK = TickSize.of("1.00")


def instrument_spec() -> InstrumentSpec:
    return InstrumentSpec.model_validate(
        {
            "trading_symbol": "MCX:CRUDEOIL26OCTFUT",
            "exchange": Exchange.MCX,
            "segment": "MCX_FO",
            "underlying": "CRUDEOIL",
            "instrument_kind": InstrumentKind.FUTURE,
            "provider_token": "tok-fut-1",
            "exchange_token": 101,
            "lot_size": 100,
            "tick_size": Decimal("1.00"),
            "price_precision": 0,
            "expiry": date(2026, 10, 17),
            "trading_session": "0900-2330",
            "source": "fixture",
            "verified_at": date(2026, 9, 1),
        }
    )


def entry_snapshot(**overrides: object) -> FeatureSnapshot:
    payload: dict[str, object] = {
        "contract": f.future_contract(),
        "market": f.quote(
            bid=Price(Decimal("6849"), COMMODITY_TICK),
            ask=Price(Decimal("6850"), COMMODITY_TICK),
            bid_size=300,
            ask_size=300,
        ),
        "derivatives": DerivativesContext(
            days_to_expiry=30,
            underlying_price=Price(Decimal("6850"), COMMODITY_TICK),
        ),
    }
    payload.update(overrides)
    return f.snapshot(**payload)


def stop_exit_snapshot(side: Side = Side.BUY) -> FeatureSnapshot:
    """A quote that breaches the protective stop for the given direction.

    A long stops out when the market falls below entry; a short stops out when it
    rises above it, so the two directions need opposite moves.
    """
    if side is Side.SELL:
        return entry_snapshot(
            market=f.quote(
                bid=Price(Decimal("6899"), COMMODITY_TICK),
                ask=Price(Decimal("6900"), COMMODITY_TICK),
            ),
        )
    return entry_snapshot(
        market=f.quote(
            bid=Price(Decimal("6799"), COMMODITY_TICK),
            ask=Price(Decimal("6800"), COMMODITY_TICK),
        ),
    )


def commodity_intent(snapshot_id: str, side: Side = Side.BUY) -> TradeIntent:
    return f.intent(
        snapshot_id=snapshot_id,
        strategy_id=STRATEGY_ID,
        underlying="CRUDEOIL",
        asset_class=AssetClass.COMMODITY,
        setup_code="COMMODITY_TREND",
        requested_risk=f.money("8000"),
        estimated_max_loss=f.money("12000"),
        exit_template=f.exit_template(stop_distance_ticks=50),
        legs=(
            IntentLeg(
                leg_id="leg-1",
                contract=f.future_contract(),
                side=side,
                ratio=1,
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class Slice3Artifacts:
    decision: RiskDecision
    decision_bytes: bytes
    entry_event: OrderEvent
    exit_event: OrderEvent


def _canonical_json_bytes(model: RiskDecision) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def run_slice3(
    store: TradingStore,
    broker: PaperBroker,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
    *,
    side: Side = Side.BUY,
) -> Slice3Artifacts:
    reservations = CapitalReservationService(store, clock=clock, id_factory=id_factory)
    reconciler = PortfolioReconciler(
        broker, store, clock=clock, id_factory=id_factory, versions=f.versions()
    )
    gateway = RiskGateway(
        account_config=ACCOUNT_CONFIG,
        risk_policy=RISK_POLICY,
        reservation_service=reservations,
        margin_preview=broker,
        clock=clock,
        id_factory=id_factory,
    )
    planner = OrderPlanPlanner(clock=clock, id_factory=id_factory)
    oms = OmsEngine(
        store,
        broker,
        clock=clock,
        id_factory=id_factory,
        rate_limiter=OrderRateLimiter(clock=clock),
    )
    trade_manager = TradeManager(
        clock=clock, id_factory=id_factory, reservation_service=reservations
    )
    controls = SafetyControls(clock=clock, id_factory=id_factory)
    entry_feature = entry_snapshot()
    boot = reconciler.boot_reconcile(ACCOUNT_ID)
    readiness = ReadinessEvaluator().evaluate(
        ReadinessRequest(
            system_state=boot.result.resulting_system_state,
            entries_blocked=boot.result.entries_blocked,
            feature_snapshot=entry_feature,
            safety_controls=controls,
            strategy_id=STRATEGY_ID,
            max_clock_drift_ms=ACCOUNT_CONFIG.config.freshness.max_clock_drift_ms,
        )
    )
    assert readiness.levels[ReadinessLevel.ENTRY_READY]
    intent = commodity_intent(entry_feature.snapshot_id, side)
    portfolio = build_broker_snapshot(
        broker,
        account_id=ACCOUNT_ID,
        versions=f.versions(),
        id_factory=id_factory,
        reserved_capital=f.money("0"),
        reconciliation_ref=boot.result.result_id,
    ).model_copy(
        update={
            "exposure": f.exposure(
                equity=f.money("5000000"),
                margin_available=f.money("5000000"),
            ),
        }
    )
    decision = gateway.evaluate(
        RiskGatewayRequest(
            intent=intent,
            feature_snapshot=entry_feature,
            portfolio_snapshot=portfolio,
            instrument=instrument_spec(),
            event_risk_state=f.event_risk_state(scope="CRUDEOIL"),
        )
    )
    assert decision.action in {RiskAction.APPROVE, RiskAction.RESIZE}
    assert decision.recalculated_max_loss != intent.estimated_max_loss
    plan = planner.build(
        OrderPlanRequest(
            intent=intent,
            decision=decision,
            feature_snapshot=entry_feature,
            account_id=ACCOUNT_ID,
        )
    )
    trade_id = trade_manager.begin_entry(intent, plan)
    submit = oms.submit_plan(plan, strategy_id=STRATEGY_ID, account_id=ACCOUNT_ID)
    entry_event = submit.events[0]
    trade_manager.apply_order_event(
        entry_event,
        intent=intent,
        capital_reservation_id=decision.capital_reservation_id,
    )
    trade_manager.register_protective_orders(
        trade_id, (plan.protective_orders[0].stub_id,)
    )
    exit_feature = stop_exit_snapshot(side)
    evaluation = trade_manager.evaluate_exit(trade_id, exit_feature, intent)
    assert evaluation.kind is ExitKind.STOP
    trade_manager.apply_exit_evaluation(trade_id, evaluation)
    exit_plan = _build_exit_plan(
        intent=intent,
        decision=decision,
        entry_event=entry_event,
        feature=exit_feature,
        account_id=ACCOUNT_ID,
        clock=clock,
        id_factory=id_factory,
    )
    exit_submit = oms.submit_plan(
        exit_plan, strategy_id=STRATEGY_ID, account_id=ACCOUNT_ID
    )
    exit_event = exit_submit.events[0]
    trade_manager.apply_exit_order_event(
        exit_event, capital_reservation_id=decision.capital_reservation_id
    )
    assert broker.get_positions() == ()
    return Slice3Artifacts(
        decision=decision,
        decision_bytes=_canonical_json_bytes(decision),
        entry_event=entry_event,
        exit_event=exit_event,
    )


def _build_exit_plan(
    *,
    intent: TradeIntent,
    decision: RiskDecision,
    entry_event: OrderEvent,
    feature: FeatureSnapshot,
    account_id: str,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> OrderPlan:
    leg = intent.legs[0]
    # Closing a position always trades the opposite way to opening it, and pays
    # the opposite side of the book: a long sells out at the bid, a short buys
    # back at the ask.
    closing_side = Side.BUY if leg.side is Side.SELL else Side.SELL
    bid = feature.market.bid
    ask = feature.market.ask
    assert bid is not None
    assert ask is not None
    limit_price = ask if closing_side is Side.BUY else bid
    internal_order_id = id_factory.new_id("ORD")
    idempotency_key = derive_idempotency_key(
        account_id=account_id,
        strategy_id=intent.strategy_id,
        strategy_version=intent.strategy_version,
        intent_id=intent.intent_id,
        leg_id=f"{leg.leg_id}-exit",
        side=closing_side.value,
        quantity_contracts=entry_event.filled_quantity,
    )
    now = clock.now_utc()
    return OrderPlan(
        plan_id=id_factory.new_id("PLAN"),
        intent_id=intent.intent_id,
        risk_decision_id=decision.decision_id,
        correlation_id=intent.correlation_id,
        policy_version=decision.policy_version,
        orders=(
            PlannedOrder(
                plan_leg_id=f"{leg.leg_id}-exit",
                leg_id=leg.leg_id,
                identity=OrderIdentity(
                    internal_order_id=internal_order_id,
                    client_order_id=internal_order_id,
                    idempotency_key=idempotency_key,
                    intent_id=intent.intent_id,
                    risk_decision_id=decision.decision_id,
                    trade_id=entry_event.identity.trade_id,
                    correlation_id=intent.correlation_id,
                    experiment_id=intent.experiment_id,
                    execution_mode=intent.execution_mode,
                ),
                command=OrderCommand(
                    contract=leg.contract,
                    side=closing_side,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.DAY,
                    quantity_contracts=entry_event.filled_quantity,
                    limit_price=limit_price,
                ),
                plan_state=OrderPlanState.RISK_APPROVED,
            ),
        ),
        protective_orders=(),
        created_at=now,
        expires_at=decision.expires_at,
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "slice3.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


@pytest.fixture
def broker(clock: FrozenClock, id_factory: SequentialIdFactory) -> PaperBroker:
    return PaperBroker.from_fixtures(
        BROKER_FIXTURES, clock=clock, id_factory=id_factory
    )


class TestSlice3CommodityFuture:
    def test_full_paper_path_with_stop_distance_sizing(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Commodity future intent sizes from stop distance and closes cleanly."""
        artifacts = run_slice3(store, broker, clock, id_factory)
        assert ReasonCode.OK in artifacts.decision.reason_codes
        assert artifacts.exit_event.state is OrderState.FILLED

    def test_short_future_paper_path_stops_out_and_closes(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """A stop-bounded short future runs the same path as a long one.

        The protective stop for a short sits above entry, so this proves the
        exit layer places it on the correct side rather than reusing the long
        geometry: only an upward move may close the position.
        """
        artifacts = run_slice3(store, broker, clock, id_factory, side=Side.SELL)
        assert artifacts.decision.permits_submission
        assert artifacts.exit_event.state is OrderState.FILLED
        assert broker.get_positions() == ()

    def test_replay_is_deterministic(self, tmp_path: Path, clock: FrozenClock) -> None:
        """Invariant 21: identical inputs reproduce the same decision bytes."""
        first_store = TradingStore.open(tmp_path / "a.sqlite", clock=clock)
        second_store = TradingStore.open(tmp_path / "b.sqlite", clock=clock)
        first_broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES, clock=clock, id_factory=SequentialIdFactory(clock.instant)
        )
        second_broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES, clock=clock, id_factory=SequentialIdFactory(clock.instant)
        )
        try:
            first = run_slice3(
                first_store, first_broker, clock, SequentialIdFactory(clock.instant)
            )
            second = run_slice3(
                second_store, second_broker, clock, SequentialIdFactory(clock.instant)
            )
        finally:
            first_store.close()
            second_store.close()
        assert first.decision_bytes == second.decision_bytes
