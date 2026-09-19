"""E2E vertical slice 5: iron condor paper path (L2-014)."""

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
    OrderType,
    ReasonCode,
    RiskAction,
    Side,
    TimeInForce,
)
from trading.domain.ids import SequentialIdFactory, derive_idempotency_key
from trading.oms import OmsEngine, OrderPlanPlanner, OrderPlanRequest, OrderRateLimiter
from trading.portfolio import PortfolioReconciler, build_broker_snapshot
from trading.risk import CapitalReservationService, RiskGateway, RiskGatewayRequest
from trading.storage.trading_store import TradingStore
from trading.trade import ExitKind, TradeManager

ROOT = Path(__file__).resolve().parent.parent
RISK_POLICY = load_risk_policy(ROOT / "config" / "risk.yaml")
ACCOUNT_CONFIG = load_config(ROOT / "config" / "base.yaml")
BROKER_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "broker"
ACCOUNT_ID = "ACC-PAPER-1"
STRATEGY_ID = "positional_index_options_poc"
NOW = f.NOW


def instrument_spec() -> InstrumentSpec:
    return InstrumentSpec.model_validate(
        {
            "trading_symbol": "NSE:NIFTY26SEP24500CE",
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
            "strike": Decimal("24500"),
            "option_type": OptionType.CALL,
            "trading_session": "0915-1530",
            "source": "fixture",
            "verified_at": date(2026, 9, 1),
        }
    )


def short_call_contract() -> ContractRef:
    return f.option_contract(symbol="NIFTY26SEP24500CE", strike=Decimal("24500"))


def long_call_contract() -> ContractRef:
    return f.option_contract(symbol="NIFTY26SEP24700CE", strike=Decimal("24700"))


def short_put_contract() -> ContractRef:
    return f.option_contract(
        symbol="NIFTY26SEP23500PE",
        strike=Decimal("23500"),
        option_type=OptionType.PUT,
    )


def long_put_contract() -> ContractRef:
    return f.option_contract(
        symbol="NIFTY26SEP23300PE",
        strike=Decimal("23300"),
        option_type=OptionType.PUT,
    )


def _leg_snapshot(
    contract: ContractRef,
    *,
    bid: str,
    ask: str,
    option_type: OptionType,
    delta: str,
) -> FeatureSnapshot:
    return f.snapshot(
        contract=contract,
        market=f.quote(bid=f.price(bid), ask=f.price(ask), bid_size=300, ask_size=300),
        derivatives=DerivativesContext(
            days_to_expiry=10,
            open_interest=5000,
            option_type=option_type,
            underlying_price=f.price("24000"),
            greeks=Greeks(
                model="bs",
                calculation_version="1",
                converged=True,
                delta=Decimal(delta),
            ),
        ),
    )


def entry_leg_snapshots() -> dict[str, FeatureSnapshot]:
    return {
        "short_call": _leg_snapshot(
            short_call_contract(),
            bid="29.95",
            ask="30.00",
            option_type=OptionType.CALL,
            delta="0.20",
        ),
        "long_call": _leg_snapshot(
            long_call_contract(),
            bid="14.95",
            ask="15.00",
            option_type=OptionType.CALL,
            delta="0.10",
        ),
        "short_put": _leg_snapshot(
            short_put_contract(),
            bid="27.95",
            ask="28.00",
            option_type=OptionType.PUT,
            delta="-0.18",
        ),
        "long_put": _leg_snapshot(
            long_put_contract(),
            bid="12.95",
            ask="13.00",
            option_type=OptionType.PUT,
            delta="-0.08",
        ),
    }


def stop_exit_snapshots() -> dict[str, FeatureSnapshot]:
    return {
        "short_call": _leg_snapshot(
            short_call_contract(),
            bid="39.95",
            ask="40.00",
            option_type=OptionType.CALL,
            delta="0.35",
        ),
        "long_call": _leg_snapshot(
            long_call_contract(),
            bid="19.95",
            ask="20.00",
            option_type=OptionType.CALL,
            delta="0.20",
        ),
        "short_put": _leg_snapshot(
            short_put_contract(),
            bid="37.95",
            ask="38.00",
            option_type=OptionType.PUT,
            delta="-0.30",
        ),
        "long_put": _leg_snapshot(
            long_put_contract(),
            bid="17.95",
            ask="18.00",
            option_type=OptionType.PUT,
            delta="-0.15",
        ),
    }


def iron_condor_intent(snapshot_id: str) -> TradeIntent:
    return f.intent(
        snapshot_id=snapshot_id,
        strategy_id=STRATEGY_ID,
        setup_code="IRON_CONDOR",
        requested_risk=f.money("15000"),
        estimated_max_loss=f.money("20000"),
        exit_template=f.exit_template(stop_distance_ticks=40),
        legs=(
            IntentLeg(
                leg_id="short_call",
                contract=short_call_contract(),
                side=Side.SELL,
                ratio=1,
            ),
            IntentLeg(
                leg_id="long_call",
                contract=long_call_contract(),
                side=Side.BUY,
                ratio=1,
            ),
            IntentLeg(
                leg_id="short_put",
                contract=short_put_contract(),
                side=Side.SELL,
                ratio=1,
            ),
            IntentLeg(
                leg_id="long_put", contract=long_put_contract(), side=Side.BUY, ratio=1
            ),
        ),
    )


@dataclass(frozen=True, slots=True)
class Slice5Artifacts:
    decision: RiskDecision
    decision_bytes: bytes
    entry_events: tuple[OrderEvent, ...]


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


def run_slice5(
    store: TradingStore,
    broker: PaperBroker,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> Slice5Artifacts:
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
    leg_snapshots = entry_leg_snapshots()
    short_call = leg_snapshots["short_call"]
    boot = reconciler.boot_reconcile(ACCOUNT_ID)
    intent = iron_condor_intent(short_call.snapshot_id)
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
            feature_snapshot=short_call,
            portfolio_snapshot=portfolio,
            instrument=instrument_spec(),
            leg_snapshots=leg_snapshots,
            event_risk_state=f.event_risk_state(),
        )
    )
    assert decision.action in {RiskAction.APPROVE, RiskAction.RESIZE}
    assert len(decision.approved_legs) == 4
    plan = planner.build(
        OrderPlanRequest(
            intent=intent,
            decision=decision,
            feature_snapshot=short_call,
            account_id=ACCOUNT_ID,
            leg_snapshots=leg_snapshots,
        )
    )
    trade_id = trade_manager.begin_entry(intent, plan)
    submit = oms.submit_plan(plan, strategy_id=STRATEGY_ID, account_id=ACCOUNT_ID)
    for event in submit.events:
        trade_manager.apply_order_event(
            event, intent=intent, capital_reservation_id=decision.capital_reservation_id
        )
    trade_manager.register_protective_orders(
        trade_id, tuple(stub.stub_id for stub in plan.protective_orders)
    )
    position = trade_manager.get_position(trade_id)
    assert position is not None
    assert position.exit_policy.scope is ExitScope.STRATEGY_PNL
    exit_features = stop_exit_snapshots()
    evaluation = trade_manager.evaluate_exit(
        trade_id,
        exit_features["short_call"],
        intent,
        leg_snapshots=exit_features,
    )
    assert evaluation.kind is ExitKind.STOP
    trade_manager.apply_exit_evaluation(trade_id, evaluation)
    exit_plan = _build_exit_plan(
        intent=intent,
        decision=decision,
        entry_events=submit.events,
        leg_snapshots=exit_features,
        account_id=ACCOUNT_ID,
        clock=clock,
        id_factory=id_factory,
    )
    exit_submit = oms.submit_plan(
        exit_plan, strategy_id=STRATEGY_ID, account_id=ACCOUNT_ID
    )
    for event in exit_submit.events:
        trade_manager.apply_exit_order_event(
            event, capital_reservation_id=decision.capital_reservation_id
        )
    assert broker.get_positions() == ()
    return Slice5Artifacts(
        decision=decision,
        decision_bytes=_canonical_json_bytes(decision),
        entry_events=submit.events,
    )


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "slice5.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


@pytest.fixture
def broker(clock: FrozenClock, id_factory: SequentialIdFactory) -> PaperBroker:
    return PaperBroker.from_fixtures(
        BROKER_FIXTURES, clock=clock, id_factory=id_factory
    )


class TestSlice5IronCondor:
    def test_full_paper_path_with_strategy_pnl_exit(
        self,
        store: TradingStore,
        broker: PaperBroker,
        clock: FrozenClock,
        id_factory: SequentialIdFactory,
    ) -> None:
        """Iron condor uses combined P&L exit scope and closes all four legs."""
        artifacts = run_slice5(store, broker, clock, id_factory)
        assert len(artifacts.entry_events) == 4
        assert ReasonCode.OK in artifacts.decision.reason_codes

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
            first = run_slice5(
                first_store, first_broker, clock, SequentialIdFactory(clock.instant)
            )
            second = run_slice5(
                second_store, second_broker, clock, SequentialIdFactory(clock.instant)
            )
        finally:
            first_store.close()
            second_store.close()
        assert first.decision_bytes == second.decision_bytes
