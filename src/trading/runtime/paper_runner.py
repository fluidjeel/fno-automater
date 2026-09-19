"""Supervised PAPER cycle: live snapshots in, paper broker out.

Invariant 2: this process never constructs a live transaction adapter.
Invariant 22: it does not write live configuration.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta

from trading.broker.paper import PaperBroker
from trading.config.evaluation import FillModelConfig
from trading.config.loader import LoadedConfig
from trading.config.risk_policy import LoadedRiskPolicy
from trading.data.cas_features import with_cas_feature_set
from trading.domain.clock import Clock
from trading.domain.contracts import (
    FeatureSnapshot,
    InstrumentSpec,
    OrderEvent,
    RiskDecision,
    RouteDecision,
    SetupFeatures,
    TradeIntent,
)
from trading.domain.contracts.common import Versions
from trading.domain.contracts.order import OrderCommand, OrderIdentity
from trading.domain.contracts.order_plan import OrderPlan, PlannedOrder
from trading.domain.contracts.position import PositionState
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import (
    ExecutionMode,
    OrderPlanState,
    OrderState,
    OrderType,
    ReasonCode,
    RiskAction,
    Side,
    SystemState,
    TimeInForce,
    TradeState,
)
from trading.domain.ids import IdFactory, derive_idempotency_key
from trading.domain.primitives import Currency, Money
from trading.news.contracts import EventRiskState
from trading.oms import OmsEngine, OrderPlanPlanner, OrderPlanRequest, OrderRateLimiter
from trading.portfolio import (
    PortfolioReconciler,
    build_broker_snapshot,
    build_portfolio_view,
)
from trading.risk import CapitalReservationService, RiskGateway, RiskGatewayRequest
from trading.runtime.isolation import assert_paper_isolation
from trading.safety import ReadinessEvaluator, ReadinessRequest, SafetyControls
from trading.storage.trading_store import TradingEventType, TradingStore
from trading.strategies import StrategyContext, build_strategy
from trading.strategies.macro import MacroAssessment
from trading.trade import TradeManager

__all__ = [
    "PaperCycleResult",
    "PaperRunner",
    "PaperStrategyOutcome",
    "PaperStrategyRequest",
]


@dataclass(frozen=True, slots=True)
class PaperStrategyRequest:
    """One strategy evaluation against injected snapshots. No network."""

    strategy_id: str
    underlying: FeatureSnapshot
    candidates: tuple[FeatureSnapshot, ...]
    instruments: Mapping[str, InstrumentSpec]
    event_risk_state: EventRiskState | None
    experiment_id: str
    execution_mode: ExecutionMode = ExecutionMode.PAPER
    macro: MacroAssessment | None = None
    execute: bool = True
    setup_features: SetupFeatures | None = None
    route_decision: RouteDecision | None = None


@dataclass(frozen=True, slots=True)
class PaperStrategyOutcome:
    """Lineage for one strategy in one cycle."""

    strategy_id: str
    snapshot_id: str
    intents: tuple[TradeIntent, ...]
    rejection_reasons: tuple[ReasonCode, ...]
    decisions: tuple[RiskDecision, ...]
    order_events: tuple[OrderEvent, ...]
    entry_blocked_reasons: tuple[ReasonCode, ...] = ()
    strategy_version: str = "unknown"
    executed: bool = True
    setup_features: SetupFeatures | None = None
    route_decision: RouteDecision | None = None
    decision_quotes: tuple[tuple[str, MarketQuote], ...] = ()
    execution_mode: ExecutionMode = ExecutionMode.PAPER


@dataclass(frozen=True, slots=True)
class PaperCycleResult:
    """Durable outputs of one supervised PAPER cycle."""

    system_state: SystemState
    outcomes: tuple[PaperStrategyOutcome, ...]
    reconcile_id: str
    entries_blocked: bool


@dataclass
class _Services:
    store: TradingStore
    broker: PaperBroker
    reconciler: PortfolioReconciler
    gateway: RiskGateway
    planner: OrderPlanPlanner
    oms: OmsEngine
    trade_manager: TradeManager
    controls: SafetyControls
    reservations: CapitalReservationService


class PaperRunner:
    """Join Layer 1 snapshots, Layer 3, Layer 2 and the paper OMS."""

    def __init__(
        self,
        *,
        account_config: LoadedConfig,
        risk_policy: LoadedRiskPolicy,
        store: TradingStore,
        broker: PaperBroker,
        clock: Clock,
        id_factory: IdFactory,
        fill_model: FillModelConfig | None = None,
        execution_mode: ExecutionMode = ExecutionMode.PAPER,
    ) -> None:
        assert_paper_isolation(
            account_config.config.environment,
            execution_mode,
            broker,
        )
        if fill_model is not None and broker.fill_model is None:
            raise ValueError(
                "fill_model was supplied but the paper broker is still in "
                "immediate-fill mode; construct PaperBroker with the same model"
            )
        self._account = account_config
        self._risk_policy = risk_policy
        self._clock = clock
        self._ids = id_factory
        self._execution_mode = execution_mode
        self._readiness = ReadinessEvaluator()
        reservations = CapitalReservationService(
            store, clock=clock, id_factory=id_factory
        )
        self._services = _Services(
            store=store,
            broker=broker,
            reconciler=PortfolioReconciler(
                broker,
                store,
                clock=clock,
                id_factory=id_factory,
                versions=_versions_from(account_config),
            ),
            gateway=RiskGateway(
                account_config=account_config,
                risk_policy=risk_policy,
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
            controls=SafetyControls(clock=clock, id_factory=id_factory),
            reservations=reservations,
        )
        self._open_book: dict[str, tuple[TradeIntent, RiskDecision]] = {}

    def run_cycle(self, requests: Sequence[PaperStrategyRequest]) -> PaperCycleResult:
        """Reconcile, evaluate each strategy, and submit only approved paper orders."""
        boot = self._services.reconciler.boot_reconcile(self._account.config.account_id)
        system_state = boot.result.resulting_system_state
        entries_blocked = boot.result.entries_blocked
        outcomes: list[PaperStrategyOutcome] = []
        for request in requests:
            assert_paper_isolation(
                self._account.config.environment,
                request.execution_mode,
                self._services.broker,
            )
            outcomes.append(
                self._run_strategy(
                    request,
                    system_state=system_state,
                    entries_blocked=entries_blocked,
                    reconcile_id=boot.result.result_id,
                )
            )
        return PaperCycleResult(
            system_state=system_state,
            outcomes=tuple(outcomes),
            reconcile_id=boot.result.result_id,
            entries_blocked=entries_blocked,
        )

    @property
    def broker(self) -> PaperBroker:
        return self._services.broker

    @property
    def trade_manager(self) -> TradeManager:
        return self._services.trade_manager

    def manage_exits(
        self, snapshots: Mapping[str, FeatureSnapshot]
    ) -> tuple[OrderEvent, ...]:
        """Evaluate stops/time exits and submit opposite LIMIT orders."""
        events: list[OrderEvent] = []
        for position in self._services.trade_manager.list_positions():
            if position.state is not TradeState.OPEN:
                continue
            book = self._open_book.get(position.trade_id)
            if book is None:
                continue
            intent, decision = book
            feature = snapshots.get(position.legs[0].contract.symbol)
            if feature is None:
                continue
            evaluation = self._services.trade_manager.evaluate_exit(
                position.trade_id, feature, intent
            )
            updated = self._services.trade_manager.apply_exit_evaluation(
                position.trade_id, evaluation
            )
            if updated.state is not TradeState.EXIT_PENDING:
                continue
            plan = self._exit_plan(intent, decision, updated, snapshots)
            if plan is None:
                continue
            submit = self._services.oms.submit_plan(
                plan,
                strategy_id=intent.strategy_id,
                account_id=self._account.config.account_id,
            )
            events.extend(submit.events)
            for event in submit.events:
                closed = self._services.trade_manager.apply_exit_order_event(
                    event,
                    capital_reservation_id=decision.capital_reservation_id,
                )
                if closed.state is TradeState.CLOSED:
                    self._open_book.pop(position.trade_id, None)
        return tuple(events)

    def _exit_plan(
        self,
        intent: TradeIntent,
        decision: RiskDecision,
        position: PositionState,
        snapshots: Mapping[str, FeatureSnapshot],
    ) -> OrderPlan | None:
        now = self._clock.now_utc()
        orders: list[PlannedOrder] = []
        for leg in position.legs:
            snapshot = snapshots.get(leg.contract.symbol)
            if snapshot is None:
                return None
            exit_side = Side.SELL if leg.side is Side.BUY else Side.BUY
            limit = (
                snapshot.market.bid if exit_side is Side.SELL else snapshot.market.ask
            )
            if limit is None:
                return None
            internal_order_id = self._ids.new_id("ORD")
            exit_leg_id = f"{leg.leg_id}-exit"
            orders.append(
                PlannedOrder(
                    plan_leg_id=exit_leg_id,
                    leg_id=leg.leg_id,
                    identity=OrderIdentity(
                        internal_order_id=internal_order_id,
                        client_order_id=internal_order_id,
                        idempotency_key=derive_idempotency_key(
                            account_id=self._account.config.account_id,
                            strategy_id=intent.strategy_id,
                            strategy_version=intent.strategy_version,
                            intent_id=intent.intent_id,
                            leg_id=exit_leg_id,
                            side=exit_side.value,
                            quantity_contracts=leg.quantity_contracts,
                        ),
                        intent_id=intent.intent_id,
                        risk_decision_id=decision.decision_id,
                        trade_id=position.trade_id,
                        correlation_id=intent.correlation_id,
                        experiment_id=intent.experiment_id,
                        execution_mode=intent.execution_mode,
                    ),
                    command=OrderCommand(
                        contract=leg.contract,
                        side=exit_side,
                        order_type=OrderType.LIMIT,
                        time_in_force=TimeInForce.DAY,
                        quantity_contracts=leg.quantity_contracts,
                        limit_price=limit,
                    ),
                    plan_state=OrderPlanState.RISK_APPROVED,
                )
            )
        if not orders:
            return None
        return OrderPlan(
            plan_id=self._ids.new_id("PLAN"),
            intent_id=intent.intent_id,
            risk_decision_id=decision.decision_id,
            correlation_id=intent.correlation_id,
            policy_version=decision.policy_version,
            orders=tuple(orders),
            protective_orders=(),
            created_at=now,
            expires_at=now + timedelta(minutes=5),
        )

    def _run_strategy(
        self,
        request: PaperStrategyRequest,
        *,
        system_state: SystemState,
        entries_blocked: bool,
        reconcile_id: str,
    ) -> PaperStrategyOutcome:
        readiness = self._readiness.evaluate(
            ReadinessRequest(
                system_state=system_state,
                entries_blocked=entries_blocked,
                feature_snapshot=request.underlying,
                safety_controls=self._services.controls,
                strategy_id=request.strategy_id,
                max_clock_drift_ms=self._account.config.freshness.max_clock_drift_ms,
            )
        )
        if not readiness.entries_permitted:
            return PaperStrategyOutcome(
                strategy_id=request.strategy_id,
                snapshot_id=request.underlying.snapshot_id,
                intents=(),
                rejection_reasons=readiness.reason_codes,
                decisions=(),
                order_events=(),
                entry_blocked_reasons=readiness.reason_codes,
                executed=request.execute,
                setup_features=request.setup_features,
                route_decision=request.route_decision,
                decision_quotes=_decision_quotes(request),
                execution_mode=request.execution_mode,
            )

        try:
            strategy = build_strategy(request.strategy_id)
        except KeyError:
            return PaperStrategyOutcome(
                strategy_id=request.strategy_id,
                snapshot_id=request.underlying.snapshot_id,
                intents=(),
                rejection_reasons=(ReasonCode.INSTRUMENT_UNKNOWN,),
                decisions=(),
                order_events=(),
                executed=request.execute,
                setup_features=request.setup_features,
                route_decision=request.route_decision,
                decision_quotes=_decision_quotes(request),
                execution_mode=request.execution_mode,
            )

        underlying = request.underlying
        if request.strategy_id == "cas_microstructure":
            stamped = with_cas_feature_set(request.underlying)
            if stamped is not None:
                underlying = stamped
        request = PaperStrategyRequest(
            strategy_id=request.strategy_id,
            underlying=underlying,
            candidates=request.candidates,
            instruments=request.instruments,
            event_risk_state=request.event_risk_state,
            experiment_id=request.experiment_id,
            execution_mode=request.execution_mode,
            macro=request.macro,
            execute=request.execute,
            setup_features=request.setup_features,
            route_decision=request.route_decision,
        )

        portfolio = build_broker_snapshot(
            self._services.broker,
            account_id=self._account.config.account_id,
            versions=_versions_from(self._account),
            id_factory=self._ids,
            reserved_capital=Money.zero(Currency.INR),
            reconciliation_ref=reconcile_id,
        )
        view = build_portfolio_view(
            portfolio,
            system_state=system_state,
            entries_blocked=entries_blocked,
        )
        decision = strategy.evaluate(
            StrategyContext(
                underlying=request.underlying,
                candidates=request.candidates,
                view=view,
                now=self._clock.now_utc(),
                experiment_id=request.experiment_id,
                execution_mode=request.execution_mode,
                macro=request.macro,
            )
        )
        intents = tuple(
            intent.model_copy(
                update={
                    "setup_features": request.setup_features,
                    "strategy_confidence": (
                        intent.strategy_confidence
                        if request.setup_features is None
                        else request.setup_features.raw_setup_score
                    ),
                }
            )
            for intent in decision.intents
        )
        rejection_reasons = tuple(item.reason for item in decision.rejections)
        if not decision.intents:
            return PaperStrategyOutcome(
                strategy_id=request.strategy_id,
                snapshot_id=request.underlying.snapshot_id,
                intents=(),
                rejection_reasons=rejection_reasons,
                decisions=(),
                order_events=(),
                strategy_version=strategy.strategy_version,
                executed=request.execute,
                setup_features=request.setup_features,
                route_decision=request.route_decision,
                decision_quotes=_decision_quotes(request),
                execution_mode=request.execution_mode,
            )

        if not request.execute:
            return PaperStrategyOutcome(
                strategy_id=request.strategy_id,
                snapshot_id=request.underlying.snapshot_id,
                intents=intents,
                rejection_reasons=rejection_reasons,
                decisions=(),
                order_events=(),
                strategy_version=strategy.strategy_version,
                executed=False,
                setup_features=request.setup_features,
                route_decision=request.route_decision,
                decision_quotes=_decision_quotes(request),
                execution_mode=request.execution_mode,
            )

        risk_decisions: list[RiskDecision] = []
        order_events: list[OrderEvent] = []
        for intent in intents:
            instrument = _instrument_for(intent, request.instruments)
            if instrument is None:
                continue
            self._publish_quotes(intent, request)
            risk = self._services.gateway.evaluate(
                RiskGatewayRequest(
                    intent=intent,
                    feature_snapshot=_feature_for(intent, request),
                    portfolio_snapshot=portfolio,
                    instrument=instrument,
                    leg_snapshots=_leg_snapshots(intent, request),
                    event_risk_state=request.event_risk_state,
                )
            )
            self._services.store.append(
                TradingEventType.RISK_DECISION,
                risk,
                event_id=risk.decision_id,
            )
            risk_decisions.append(risk)
            if risk.action not in {RiskAction.APPROVE, RiskAction.RESIZE}:
                continue
            if not risk.permits_submission:
                continue
            events = self._submit(intent, risk, request)
            order_events.extend(events)
        return PaperStrategyOutcome(
            strategy_id=request.strategy_id,
            snapshot_id=request.underlying.snapshot_id,
            intents=intents,
            rejection_reasons=rejection_reasons,
            decisions=tuple(risk_decisions),
            order_events=tuple(order_events),
            strategy_version=strategy.strategy_version,
            executed=True,
            setup_features=request.setup_features,
            route_decision=request.route_decision,
            decision_quotes=_decision_quotes(request),
            execution_mode=request.execution_mode,
        )

    def _submit(
        self,
        intent: TradeIntent,
        risk: RiskDecision,
        request: PaperStrategyRequest,
    ) -> tuple[OrderEvent, ...]:
        feature = _feature_for(intent, request)
        plan = self._services.planner.build(
            OrderPlanRequest(
                intent=intent,
                decision=risk,
                feature_snapshot=feature,
                account_id=self._account.config.account_id,
                leg_snapshots=_leg_snapshots(intent, request),
            )
        )
        trade_id = self._services.trade_manager.begin_entry(intent, plan)
        submit = self._services.oms.submit_plan(
            plan,
            strategy_id=intent.strategy_id,
            account_id=self._account.config.account_id,
        )
        for event in submit.events:
            if event.state is OrderState.FILLED:
                position = self._services.trade_manager.apply_order_event(
                    event,
                    intent=intent,
                    capital_reservation_id=risk.capital_reservation_id,
                )
                if (
                    position.state is TradeState.OPENING
                    or position.state.requires_protective_coverage
                ) and plan.protective_orders:
                    self._services.trade_manager.register_protective_orders(
                        trade_id,
                        tuple(stub.stub_id for stub in plan.protective_orders),
                    )
                self._open_book[trade_id] = (intent, risk)
        return submit.events

    def _publish_quotes(
        self, intent: TradeIntent, request: PaperStrategyRequest
    ) -> None:
        by_symbol = {
            snap.contract.symbol: snap.market
            for snap in (request.underlying, *request.candidates)
        }
        for leg in intent.legs:
            quote = by_symbol.get(leg.contract.symbol)
            if quote is not None:
                self._services.broker.publish_quote(leg.contract.symbol, quote)


def _decision_quotes(
    request: PaperStrategyRequest,
) -> tuple[tuple[str, MarketQuote], ...]:
    return tuple(
        (snapshot.contract.symbol, snapshot.market) for snapshot in request.candidates
    )


def _instrument_for(
    intent: TradeIntent, instruments: Mapping[str, InstrumentSpec]
) -> InstrumentSpec | None:
    symbol = intent.legs[0].contract.symbol
    return instruments.get(symbol)


def _feature_for(intent: TradeIntent, request: PaperStrategyRequest) -> FeatureSnapshot:
    """Use the priced leg book; align snapshot_id with the intent Layer 3 stamped."""
    priced = _priced_snapshot(intent, request)
    if priced.snapshot_id == intent.snapshot_id:
        return priced
    return priced.model_copy(update={"snapshot_id": intent.snapshot_id})


def _priced_snapshot(
    intent: TradeIntent, request: PaperStrategyRequest
) -> FeatureSnapshot:
    symbol = intent.legs[0].contract.symbol
    for snap in request.candidates:
        if snap.contract.symbol == symbol:
            return snap
    if request.underlying.snapshot_id == intent.snapshot_id:
        return request.underlying
    return request.underlying


def _leg_snapshots(
    intent: TradeIntent, request: PaperStrategyRequest
) -> dict[str, FeatureSnapshot]:
    by_symbol = {snap.contract.symbol: snap for snap in request.candidates}
    mapping: dict[str, FeatureSnapshot] = {}
    for leg in intent.legs:
        snap = by_symbol.get(leg.contract.symbol)
        if snap is not None:
            mapping[leg.leg_id] = snap
    return mapping


def _versions_from(account_config: LoadedConfig) -> Versions:
    return Versions(
        code_version="0.1.0",
        config_version=account_config.version,
        config_checksum=account_config.checksum,
    )
