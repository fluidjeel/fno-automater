"""E2E vertical slice 2: bull call debit spread paper path (L2-011).

Invariant 4: max loss is recalculated from net debit, not copied from the intent.
Invariant 25: every decision, order and recovery transition is durably auditable.
"""

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
    ContractRef,
    DerivativesContext,
    FeatureSnapshot,
    Greeks,
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
    Exchange,
    ExitScope,
    InstrumentKind,
    OptionType,
    OrderPlanState,
    OrderState,
    OrderType,
    ReadinessLevel,
    ReasonCode,
    ReservationState,
    RiskAction,
    Side,
    SystemState,
    TimeInForce,
    TradeState,
)
from trading.domain.ids import SequentialIdFactory, derive_idempotency_key
from trading.oms import OmsEngine, OrderPlanPlanner, OrderPlanRequest, OrderRateLimiter
from trading.portfolio import PortfolioReconciler, build_broker_snapshot
from trading.risk import CapitalReservationService, RiskGateway, RiskGatewayRequest
from trading.safety import ReadinessEvaluator, ReadinessRequest, SafetyControls
from trading.storage.trading_store import TradingEventType, TradingStore
from trading.trade import ExitKind, TradeManager

ROOT = Path(__file__).resolve().parent.parent
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
ACCOUNT_ID = "ACC-PAPER-1"
STRATEGY_ID = "positional_index_options_poc"
NOW = f.NOW


def long_contract() -> ContractRef:
    return f.option_contract(symbol="NIFTY26SEP24000CE", strike=Decimal("24000"))


def short_contract() -> ContractRef:
    return f.option_contract(symbol="NIFTY26SEP24200CE", strike=Decimal("24200"))


def instrument_spec() -> InstrumentSpec:
    return InstrumentSpec.model_validate(
        {
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
    )


def long_snapshot(**overrides: object) -> FeatureSnapshot:
    payload: dict[str, object] = {
        "contract": long_contract(),
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
            greeks=Greeks(
                model="bs",
                calculation_version="1",
                converged=True,
                delta=Decimal("0.55"),
            ),
        ),
    }
    payload.update(overrides)
    return f.snapshot(**payload)


def short_snapshot(**overrides: object) -> FeatureSnapshot:
    payload: dict[str, object] = {
        "contract": short_contract(),
        "market": f.quote(
            bid=f.price("44.95"),
            ask=f.price("45.00"),
            bid_size=300,
            ask_size=300,
        ),
        "derivatives": DerivativesContext(
            days_to_expiry=10,
            open_interest=4000,
            option_type=OptionType.CALL,
            underlying_price=f.price("24000"),
            greeks=Greeks(
                model="bs",
                calculation_version="1",
                converged=True,
                delta=Decimal("0.35"),
            ),
        ),
    }
    payload.update(overrides)
    return f.snapshot(**payload)


def debit_spread_intent(snapshot_id: str) -> TradeIntent:
    return f.intent(
        snapshot_id=snapshot_id,
        strategy_id=STRATEGY_ID,
        setup_code="BULL_CALL_DEBIT",
        requested_risk=f.money("4000"),
        estimated_max_loss=f.money("6000"),
        exit_template=f.exit_template(stop_distance_ticks=40),
        legs=(
            IntentLeg(
                leg_id="long",
                contract=long_contract(),
                side=Side.BUY,
                ratio=1,
            ),
            IntentLeg(
                leg_id="short",
                contract=short_contract(),
                side=Side.SELL,
                ratio=1,
            ),
        ),
    )


def stop_exit_snapshots() -> dict[str, FeatureSnapshot]:
    return {
        "long": long_snapshot(
            market=f.quote(bid=f.price("89.95"), ask=f.price("90.00")),
        ),
        "short": short_snapshot(
            market=f.quote(bid=f.price("43.95"), ask=f.price("44.00")),
        ),
    }


@dataclass(frozen=True, slots=True)
class Slice2Artifacts:
    decision: RiskDecision
    decision_bytes: bytes
    entry_events: tuple[OrderEvent, ...]
    exit_events: tuple[OrderEvent, ...]
    boot_reconcile_id: str
    post_reconcile_id: str


def _canonical_json_bytes(model: RiskDecision) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


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
        if reference is None:
            raise ValueError(f"exit snapshot for {leg.leg_id} lacks a quote")
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


@dataclass(frozen=True, slots=True)
class _Slice2Services:
    store: TradingStore
    broker: PaperBroker
    clock: FrozenClock
    id_factory: SequentialIdFactory
    reconciler: PortfolioReconciler
    gateway: RiskGateway
    planner: OrderPlanPlanner
    oms: OmsEngine
    trade_manager: TradeManager


def _build_services(
    store: TradingStore,
    broker: PaperBroker,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> _Slice2Services:
    reservations = CapitalReservationService(
        store,
        clock=clock,
        id_factory=id_factory,
    )
    return _Slice2Services(
        store=store,
        broker=broker,
        clock=clock,
        id_factory=id_factory,
        reconciler=PortfolioReconciler(
            broker,
            store,
            clock=clock,
            id_factory=id_factory,
            versions=f.versions(),
        ),
        gateway=RiskGateway(
            account_config=ACCOUNT_CONFIG,
            risk_policy=RISK_POLICY,
            reservation_service=reservations,
            margin_preview=broker,
            clock=clock,
            id_factory=id_factory,
        ),
        planner=OrderPlanPlanner(clock=clock, id_factory=id_factory),
        oms=OmsEngine(
            store,
            broker,
            clock=clock,
            id_factory=id_factory,
            rate_limiter=OrderRateLimiter(clock=clock),
        ),
        trade_manager=TradeManager(
            clock=clock,
            id_factory=id_factory,
            reservation_service=reservations,
        ),
    )


def run_slice2(
    store: TradingStore,
    broker: PaperBroker,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> Slice2Artifacts:
    """Execute the slice-2 debit spread paper path end to end."""
    services = _build_services(store, broker, clock, id_factory)
    controls = SafetyControls(clock=clock, id_factory=id_factory)
    long_feature = long_snapshot()
    short_feature = short_snapshot()
    leg_snapshots = {"long": long_feature, "short": short_feature}

    boot = services.reconciler.boot_reconcile(ACCOUNT_ID)
    assert boot.result.resulting_system_state is SystemState.READY
    readiness = ReadinessEvaluator().evaluate(
        ReadinessRequest(
            system_state=boot.result.resulting_system_state,
            entries_blocked=boot.result.entries_blocked,
            feature_snapshot=long_feature,
            safety_controls=controls,
            strategy_id=STRATEGY_ID,
            max_clock_drift_ms=ACCOUNT_CONFIG.config.freshness.max_clock_drift_ms,
        )
    )
    assert readiness.levels[ReadinessLevel.ENTRY_READY]

    intent = debit_spread_intent(long_feature.snapshot_id)
    portfolio = build_broker_snapshot(
        services.broker,
        account_id=ACCOUNT_ID,
        versions=f.versions(),
        id_factory=services.id_factory,
        reserved_capital=f.money("0"),
        reconciliation_ref=boot.result.result_id,
    )
    decision = services.gateway.evaluate(
        RiskGatewayRequest(
            intent=intent,
            feature_snapshot=long_feature,
            portfolio_snapshot=portfolio,
            instrument=instrument_spec(),
            leg_snapshots=leg_snapshots,
            event_risk_state=f.event_risk_state(),
        )
    )
    assert decision.action in {RiskAction.APPROVE, RiskAction.RESIZE}
    assert decision.permits_submission
    assert len(decision.approved_legs) == 2
    assert decision.recalculated_max_loss != intent.estimated_max_loss
    services.store.append(
        TradingEventType.RISK_DECISION,
        decision,
        event_id=decision.decision_id,
    )

    plan = services.planner.build(
        OrderPlanRequest(
            intent=intent,
            decision=decision,
            feature_snapshot=long_feature,
            account_id=ACCOUNT_ID,
            leg_snapshots=leg_snapshots,
        )
    )
    assert len(plan.orders) == 2
    trade_ids = {order.identity.trade_id for order in plan.orders}
    assert len(trade_ids) == 1

    trade_id = services.trade_manager.begin_entry(intent, plan)
    submit = services.oms.submit_plan(
        plan,
        strategy_id=STRATEGY_ID,
        account_id=ACCOUNT_ID,
    )
    assert len(submit.events) == 2
    for event in submit.events:
        assert event.state is OrderState.FILLED
        services.trade_manager.apply_order_event(
            event,
            intent=intent,
            capital_reservation_id=decision.capital_reservation_id,
        )
    assert decision.capital_reservation_id is not None
    reservation = services.store.get_reservation(decision.capital_reservation_id)
    assert reservation is not None
    assert reservation.state is ReservationState.COMMITTED

    stub_ids = tuple(stub.stub_id for stub in plan.protective_orders)
    open_position = services.trade_manager.register_protective_orders(
        trade_id,
        stub_ids,
    )
    assert open_position.state is TradeState.OPEN
    assert len(open_position.legs) == 2
    assert open_position.exit_policy.scope is ExitScope.STRATEGY_PNL

    exit_features = stop_exit_snapshots()
    evaluation = services.trade_manager.evaluate_exit(
        trade_id,
        exit_features["long"],
        intent,
        leg_snapshots=exit_features,
    )
    assert evaluation.kind is ExitKind.STOP
    services.trade_manager.apply_exit_evaluation(trade_id, evaluation)
    exit_plan = _build_exit_plan(
        intent=intent,
        decision=decision,
        entry_events=submit.events,
        leg_snapshots=exit_features,
        account_id=ACCOUNT_ID,
        clock=services.clock,
        id_factory=services.id_factory,
    )
    exit_submit = services.oms.submit_plan(
        exit_plan,
        strategy_id=STRATEGY_ID,
        account_id=ACCOUNT_ID,
    )
    for event in exit_submit.events:
        assert event.state is OrderState.FILLED
        services.trade_manager.apply_exit_order_event(
            event,
            capital_reservation_id=decision.capital_reservation_id,
        )
    assert services.broker.get_positions() == ()
    released = services.store.get_reservation(decision.capital_reservation_id)
    assert released is not None
    assert released.state is ReservationState.RELEASED

    post = services.reconciler.boot_reconcile(ACCOUNT_ID)
    assert post.result.entries_blocked is False
    return Slice2Artifacts(
        decision=decision,
        decision_bytes=_canonical_json_bytes(decision),
        entry_events=submit.events,
        exit_events=exit_submit.events,
        boot_reconcile_id=boot.result.result_id,
        post_reconcile_id=post.result.result_id,
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "slice2.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


@pytest.fixture
def broker(clock: FrozenClock, id_factory: SequentialIdFactory) -> PaperBroker:
    return PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=id_factory,
    )


class TestSlice2DebitSpread:
    def test_full_paper_path_with_multi_leg_order_plan(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Debit spread intent produces a two-leg plan and closes cleanly."""
        artifacts = run_slice2(store, broker, clock, id_factory)

        stored_types = [event.event_type for event in store.read_events()]
        assert TradingEventType.RISK_DECISION in stored_types
        assert TradingEventType.ORDER_EVENT in stored_types
        assert TradingEventType.CAPITAL_RESERVATION in stored_types
        assert len(artifacts.entry_events) == 2
        assert artifacts.entry_events[0].identity.trade_id == (
            artifacts.entry_events[1].identity.trade_id
        )
        assert ReasonCode.OK in artifacts.decision.reason_codes

    def test_replay_is_deterministic(
        self,
        tmp_path: Path,
        clock: FrozenClock,
    ) -> None:
        """Invariant 21: identical inputs reproduce the same decision bytes."""
        first_store = TradingStore.open(tmp_path / "run-a.sqlite", clock=clock)
        second_store = TradingStore.open(tmp_path / "run-b.sqlite", clock=clock)
        first_broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES,
            clock=clock,
            id_factory=SequentialIdFactory(clock.instant),
        )
        second_broker = PaperBroker.from_fixtures(
            BROKER_FIXTURES,
            clock=clock,
            id_factory=SequentialIdFactory(clock.instant),
        )
        try:
            first = run_slice2(
                first_store,
                first_broker,
                clock,
                SequentialIdFactory(clock.instant),
            )
            second = run_slice2(
                second_store,
                second_broker,
                clock,
                SequentialIdFactory(clock.instant),
            )
        finally:
            first_store.close()
            second_store.close()

        assert first.decision_bytes == second.decision_bytes
        assert (
            first.entry_events[0].identity.idempotency_key
            == second.entry_events[0].identity.idempotency_key
        )
