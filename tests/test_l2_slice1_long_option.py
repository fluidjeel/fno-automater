"""E2E vertical slice 1: long call/put paper path (L2-010).

Invariant 25: every decision, order and recovery transition is durably auditable.
Invariant 21: same inputs reproduce byte-identical RiskDecision serialization.
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
    DerivativesContext,
    FeatureSnapshot,
    InstrumentSpec,
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


def entry_snapshot(**overrides: object) -> FeatureSnapshot:
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


def stop_exit_snapshot() -> FeatureSnapshot:
    return entry_snapshot(
        market=f.quote(bid=f.price("89.95"), ask=f.price("90.00")),
    )


@dataclass(frozen=True, slots=True)
class Slice1Artifacts:
    """Durable outputs from one full slice-1 run."""

    decision: RiskDecision
    decision_bytes: bytes
    entry_event: OrderEvent
    exit_event: OrderEvent
    boot_reconcile_id: str
    post_reconcile_id: str
    event_sequences: tuple[int, ...]


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
    entry_event: OrderEvent,
    feature: FeatureSnapshot,
    account_id: str,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> OrderPlan:
    position_leg = intent.legs[0]
    bid = feature.market.bid
    if bid is None:
        raise ValueError("exit snapshot requires a bid")
    quantity = entry_event.filled_quantity
    trade_id = entry_event.identity.trade_id
    internal_order_id = id_factory.new_id("ORD")
    idempotency_key = derive_idempotency_key(
        account_id=account_id,
        strategy_id=intent.strategy_id,
        strategy_version=intent.strategy_version,
        intent_id=intent.intent_id,
        leg_id=f"{position_leg.leg_id}-exit",
        side=Side.SELL.value,
        quantity_contracts=quantity,
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
                plan_leg_id=f"{position_leg.leg_id}-exit",
                leg_id=position_leg.leg_id,
                identity=OrderIdentity(
                    internal_order_id=internal_order_id,
                    client_order_id=internal_order_id,
                    idempotency_key=idempotency_key,
                    intent_id=intent.intent_id,
                    risk_decision_id=decision.decision_id,
                    trade_id=trade_id,
                    correlation_id=intent.correlation_id,
                ),
                command=OrderCommand(
                    contract=position_leg.contract,
                    side=Side.SELL,
                    order_type=OrderType.LIMIT,
                    time_in_force=TimeInForce.DAY,
                    quantity_contracts=quantity,
                    limit_price=bid,
                ),
                plan_state=OrderPlanState.RISK_APPROVED,
            ),
        ),
        protective_orders=(),
        created_at=now,
        expires_at=decision.expires_at,
    )


@dataclass(frozen=True, slots=True)
class _Slice1Services:
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
) -> _Slice1Services:
    reservations = CapitalReservationService(
        store,
        clock=clock,
        id_factory=id_factory,
    )
    return _Slice1Services(
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


def _approve_intent(
    services: _Slice1Services,
    *,
    boot_reconcile_id: str,
) -> tuple[TradeIntent, FeatureSnapshot, RiskDecision]:
    entry_feature = entry_snapshot()
    intent = f.intent(
        snapshot_id=entry_feature.snapshot_id,
        strategy_id=STRATEGY_ID,
        requested_risk=f.money("6500"),
        estimated_max_loss=f.money("10000"),
        exit_template=f.exit_template(stop_distance_ticks=40),
    )
    portfolio = build_broker_snapshot(
        services.broker,
        account_id=ACCOUNT_ID,
        versions=f.versions(),
        id_factory=services.id_factory,
        reserved_capital=f.money("0"),
        reconciliation_ref=boot_reconcile_id,
    )
    decision = services.gateway.evaluate(
        RiskGatewayRequest(
            intent=intent,
            feature_snapshot=entry_feature,
            portfolio_snapshot=portfolio,
            instrument=instrument_spec(),
        )
    )
    assert decision.action in {RiskAction.APPROVE, RiskAction.RESIZE}
    assert decision.permits_submission
    services.store.append(
        TradingEventType.RISK_DECISION,
        decision,
        event_id=decision.decision_id,
    )
    return intent, entry_feature, decision


def _enter_position(
    services: _Slice1Services,
    *,
    intent: TradeIntent,
    entry_feature: FeatureSnapshot,
    decision: RiskDecision,
) -> tuple[str, OrderEvent]:
    plan = services.planner.build(
        OrderPlanRequest(
            intent=intent,
            decision=decision,
            feature_snapshot=entry_feature,
            account_id=ACCOUNT_ID,
        )
    )
    trade_id = services.trade_manager.begin_entry(intent, plan)
    submit = services.oms.submit_plan(
        plan,
        strategy_id=STRATEGY_ID,
        account_id=ACCOUNT_ID,
    )
    entry_event = submit.events[0]
    assert entry_event.state is OrderState.FILLED
    services.trade_manager.apply_order_event(
        entry_event,
        intent=intent,
        capital_reservation_id=decision.capital_reservation_id,
    )
    assert decision.capital_reservation_id is not None
    reservation = services.store.get_reservation(decision.capital_reservation_id)
    assert reservation is not None
    assert reservation.state is ReservationState.COMMITTED
    open_position = services.trade_manager.register_protective_orders(
        trade_id,
        (plan.protective_orders[0].stub_id,),
    )
    assert open_position.state is TradeState.OPEN
    return trade_id, entry_event


def _exit_position(
    services: _Slice1Services,
    *,
    trade_id: str,
    intent: TradeIntent,
    decision: RiskDecision,
    entry_event: OrderEvent,
) -> OrderEvent:
    exit_feature = stop_exit_snapshot()
    evaluation = services.trade_manager.evaluate_exit(trade_id, exit_feature, intent)
    assert evaluation.kind is ExitKind.STOP
    assert evaluation.should_exit
    services.trade_manager.apply_exit_evaluation(trade_id, evaluation)
    exit_plan = _build_exit_plan(
        intent=intent,
        decision=decision,
        entry_event=entry_event,
        feature=exit_feature,
        account_id=ACCOUNT_ID,
        clock=services.clock,
        id_factory=services.id_factory,
    )
    exit_submit = services.oms.submit_plan(
        exit_plan,
        strategy_id=STRATEGY_ID,
        account_id=ACCOUNT_ID,
    )
    exit_event = exit_submit.events[0]
    assert exit_event.state is OrderState.FILLED
    closed = services.trade_manager.apply_exit_order_event(
        exit_event,
        capital_reservation_id=decision.capital_reservation_id,
    )
    assert closed.state is TradeState.CLOSED
    assert services.broker.get_positions() == ()
    assert decision.capital_reservation_id is not None
    released = services.store.get_reservation(decision.capital_reservation_id)
    assert released is not None
    assert released.state is ReservationState.RELEASED
    return exit_event


def run_slice1(
    store: TradingStore,
    broker: PaperBroker,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> Slice1Artifacts:
    """Execute the slice-1 paper path end to end."""
    services = _build_services(store, broker, clock, id_factory)
    controls = SafetyControls(clock=clock, id_factory=id_factory)
    boot = services.reconciler.boot_reconcile(ACCOUNT_ID)
    assert boot.result.resulting_system_state is SystemState.READY
    readiness = ReadinessEvaluator().evaluate(
        ReadinessRequest(
            system_state=boot.result.resulting_system_state,
            entries_blocked=boot.result.entries_blocked,
            feature_snapshot=entry_snapshot(),
            safety_controls=controls,
            strategy_id=STRATEGY_ID,
            max_clock_drift_ms=ACCOUNT_CONFIG.config.freshness.max_clock_drift_ms,
        )
    )
    assert readiness.levels[ReadinessLevel.ENTRY_READY]
    intent, entry_feature, decision = _approve_intent(
        services,
        boot_reconcile_id=boot.result.result_id,
    )
    trade_id, entry_event = _enter_position(
        services,
        intent=intent,
        entry_feature=entry_feature,
        decision=decision,
    )
    exit_event = _exit_position(
        services,
        trade_id=trade_id,
        intent=intent,
        decision=decision,
        entry_event=entry_event,
    )
    post = services.reconciler.boot_reconcile(ACCOUNT_ID)
    assert post.result.entries_blocked is False
    assert post.result.resulting_system_state is SystemState.READY
    sequences = tuple(
        stored.sequence for stored in store.read_events() if stored.sequence > 0
    )
    return Slice1Artifacts(
        decision=decision,
        decision_bytes=_canonical_json_bytes(decision),
        entry_event=entry_event,
        exit_event=exit_event,
        boot_reconcile_id=boot.result.result_id,
        post_reconcile_id=post.result.result_id,
        event_sequences=sequences,
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "slice1.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


@pytest.fixture
def broker(clock: FrozenClock, id_factory: SequentialIdFactory) -> PaperBroker:
    return PaperBroker.from_fixtures(
        BROKER_FIXTURES,
        clock=clock,
        id_factory=id_factory,
    )


class TestSlice1LongOption:
    def test_full_paper_path_with_audit_lineage(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Intent through exit leaves a durable audit trail."""
        artifacts = run_slice1(store, broker, clock, id_factory)

        stored_types = [event.event_type for event in store.read_events()]
        assert TradingEventType.RISK_DECISION in stored_types
        assert TradingEventType.ORDER_EVENT in stored_types
        assert TradingEventType.RECONCILIATION_EVENT in stored_types
        assert TradingEventType.CAPITAL_RESERVATION in stored_types

        entry = artifacts.entry_event
        assert entry.identity.intent_id == artifacts.decision.intent_id
        assert entry.identity.risk_decision_id == artifacts.decision.decision_id
        assert artifacts.exit_event.identity.trade_id == entry.identity.trade_id
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
            first = run_slice1(
                first_store,
                first_broker,
                clock,
                SequentialIdFactory(clock.instant),
            )
            second = run_slice1(
                second_store,
                second_broker,
                clock,
                SequentialIdFactory(clock.instant),
            )
        finally:
            first_store.close()
            second_store.close()

        assert first.decision_bytes == second.decision_bytes
        assert first.decision.action == second.decision.action
        assert (
            first.entry_event.identity.idempotency_key
            == second.entry_event.identity.idempotency_key
        )
        assert len(first.event_sequences) == len(second.event_sequences)
