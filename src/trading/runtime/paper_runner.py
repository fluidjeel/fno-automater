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
    PositionLifecycleRecord,
    ReconciliationEvent,
    RiskDecision,
    RouteDecision,
    SetupFeatures,
    TradeIntent,
)
from trading.domain.contracts.common import Versions
from trading.domain.contracts.order import OrderCommand, OrderIdentity
from trading.domain.contracts.order_plan import OrderPlan, PlannedOrder
from trading.domain.contracts.portfolio import PositionRecord
from trading.domain.contracts.position import PositionState
from trading.domain.contracts.snapshot import MarketQuote
from trading.domain.enums import (
    DifferenceClass,
    ExecutionMode,
    ExitScope,
    HoldingStyle,
    OrderPlanState,
    OrderState,
    OrderType,
    ReasonCode,
    ReconciliationTrigger,
    RiskAction,
    Severity,
    Side,
    SystemState,
    TimeInForce,
    TradeState,
    Trigger,
)
from trading.domain.ids import IdFactory, derive_idempotency_key
from trading.domain.primitives import Currency, Money
from trading.news.contracts import EventRiskState
from trading.oms import (
    OmsEngine,
    OrderFrozenError,
    OrderPlanPlanner,
    OrderPlanRequest,
    OrderRateLimiter,
)
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
from trading.trade import TradeManager, monitor_leg

__all__ = [
    "LifecycleAlert",
    "PaperCycleResult",
    "PaperRunner",
    "PaperStrategyOutcome",
    "PaperStrategyRequest",
    "PositionRecoveryResult",
]


@dataclass(frozen=True, slots=True)
class LifecycleAlert:
    """One unreconciled or unprotected open-position finding."""

    trade_id: str
    reason_code: ReasonCode
    detail: str


@dataclass(frozen=True, slots=True)
class PositionRecoveryResult:
    """Outcome of restoring persisted PAPER position lifecycles."""

    restored_trade_ids: tuple[str, ...]
    entries_blocked: bool
    alerts: tuple[LifecycleAlert, ...]
    unprotected_trade_ids: tuple[str, ...]
    unreconciled_trade_ids: tuple[str, ...]


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
    route_decision: RouteDecision | None = None


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
        self._lifecycle_recovered = False
        self._last_recovery = PositionRecoveryResult(
            restored_trade_ids=(),
            entries_blocked=False,
            alerts=(),
            unprotected_trade_ids=(),
            unreconciled_trade_ids=(),
        )

    def run_cycle(self, requests: Sequence[PaperStrategyRequest]) -> PaperCycleResult:
        """Reconcile, evaluate each strategy, and submit only approved paper orders."""
        recovery = self.recover_lifecycle()
        boot = self._services.reconciler.boot_reconcile(self._account.config.account_id)
        system_state = boot.result.resulting_system_state
        entries_blocked = boot.result.entries_blocked or recovery.entries_blocked
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
        route_decision = next(
            (
                request.route_decision
                for request in requests
                if request.route_decision is not None
            ),
            None,
        )
        return PaperCycleResult(
            system_state=system_state,
            outcomes=tuple(outcomes),
            reconcile_id=boot.result.result_id,
            entries_blocked=entries_blocked,
            route_decision=route_decision,
        )

    @property
    def broker(self) -> PaperBroker:
        return self._services.broker

    @property
    def trade_manager(self) -> TradeManager:
        return self._services.trade_manager

    @property
    def last_recovery(self) -> PositionRecoveryResult:
        return self._last_recovery

    def recover_lifecycle(self) -> PositionRecoveryResult:
        """Restore persisted positions and block entries on unresolved gaps.

        Invariant 9: restart begins in recovery; new entries wait. Protective
        stub IDs are local software coverage, not broker-resident PAPER stops.
        """
        if self._lifecycle_recovered:
            return self._last_recovery
        broker_by_trade: dict[str, list[PositionRecord]] = {}
        for broker_position in self._services.broker.get_positions():
            broker_by_trade.setdefault(broker_position.trade_id, []).append(
                broker_position
            )
        restored: list[str] = []
        alerts: list[LifecycleAlert] = []
        unprotected: list[str] = []
        unreconciled: list[str] = []
        active: list[PositionLifecycleRecord] = []
        for persisted in self._services.store.list_position_lifecycle():
            if persisted.position.state is TradeState.CLOSED:
                continue
            active.append(persisted)
            self._services.trade_manager.restore_position(persisted.position)
            self._open_book[persisted.trade_id] = (
                persisted.intent,
                persisted.risk_decision,
            )
            restored.append(persisted.trade_id)
        active_ids = {item.trade_id for item in active}
        for persisted in active:
            broker_legs = tuple(broker_by_trade.get(persisted.trade_id, ()))
            for alert in _lifecycle_issues(persisted, broker_legs):
                alerts.append(alert)
                if alert.reason_code is ReasonCode.PROTECTIVE_COVERAGE_MISSING:
                    unprotected.append(persisted.trade_id)
                else:
                    unreconciled.append(persisted.trade_id)
            for order_id in persisted.exit_order_ids:
                event = self._exit_order_event(order_id)
                if event is None or event.state is OrderState.UNKNOWN:
                    alerts.append(
                        LifecycleAlert(
                            trade_id=persisted.trade_id,
                            reason_code=ReasonCode.RECONCILIATION_UNRESOLVED,
                            detail=(
                                "exit order status is unknown; replacement is "
                                "blocked until reconciliation"
                            ),
                        )
                    )
                    unreconciled.append(persisted.trade_id)
                    break
        for trade_id in sorted(set(broker_by_trade) - active_ids):
            alerts.append(
                LifecycleAlert(
                    trade_id=trade_id,
                    reason_code=ReasonCode.RECONCILIATION_UNRESOLVED,
                    detail="broker position has no persisted exit lifecycle",
                )
            )
            unreconciled.append(trade_id)
        unique_alerts = tuple(_unique_alerts(alerts))
        entries_blocked = bool(unique_alerts)
        if entries_blocked:
            self._services.controls.freeze_entries(
                actor="paper-lifecycle",
                scope="paper/position-lifecycle",
                trigger=Trigger.RECONCILIATION,
                incident_id=self._ids.new_id("INC"),
            )
            self._audit_lifecycle_alerts(unique_alerts)
        result = PositionRecoveryResult(
            restored_trade_ids=tuple(restored),
            entries_blocked=entries_blocked,
            alerts=unique_alerts,
            unprotected_trade_ids=tuple(dict.fromkeys(unprotected)),
            unreconciled_trade_ids=tuple(dict.fromkeys(unreconciled)),
        )
        self._last_recovery = result
        self._lifecycle_recovered = True
        return result

    def flush_lifecycle(self) -> None:
        """Persist the in-memory open book (EOD / process halt)."""
        for trade_id in tuple(self._open_book):
            self._write_lifecycle(trade_id)

    def manage_exits(
        self, snapshots: Mapping[str, FeatureSnapshot]
    ) -> tuple[OrderEvent, ...]:
        """Evaluate frozen exit policy and submit opposite LIMIT orders."""
        events: list[OrderEvent] = []
        events.extend(self._resume_inflight_exits(snapshots))
        for position in self._services.trade_manager.list_positions():
            if position.state is not TradeState.OPEN:
                continue
            book = self._open_book.get(position.trade_id)
            if book is None:
                continue
            intent, decision = book
            leg_snapshots = self._exit_leg_snapshots(position, intent, snapshots)
            if leg_snapshots is None:
                continue
            feature = _monitor_snapshot(intent, leg_snapshots)
            if feature is None:
                continue
            evaluation = self._services.trade_manager.evaluate_exit(
                position.trade_id,
                feature,
                intent,
                leg_snapshots=leg_snapshots,
            )
            if not evaluation.should_exit and evaluation.updated_policy is None:
                continue
            updated = self._services.trade_manager.apply_exit_evaluation(
                position.trade_id, evaluation
            )
            self._write_lifecycle(position.trade_id)
            if updated.state is not TradeState.EXIT_PENDING:
                continue
            events.extend(self._submit_exit(intent, decision, updated, snapshots))
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

    def _resume_inflight_exits(
        self, snapshots: Mapping[str, FeatureSnapshot]
    ) -> tuple[OrderEvent, ...]:
        events: list[OrderEvent] = []
        for position in self._services.trade_manager.list_positions():
            if position.state not in {TradeState.EXIT_PENDING, TradeState.CLOSING}:
                continue
            book = self._open_book.get(position.trade_id)
            if book is None:
                continue
            intent, decision = book
            record = self._services.store.get_position_lifecycle(position.trade_id)
            if record is not None and record.exit_order_ids:
                events.extend(self._adopt_existing_exit_orders(record))
                continue
            events.extend(self._submit_exit(intent, decision, position, snapshots))
        return tuple(events)

    def _submit_exit(
        self,
        intent: TradeIntent,
        decision: RiskDecision,
        position: PositionState,
        snapshots: Mapping[str, FeatureSnapshot],
    ) -> tuple[OrderEvent, ...]:
        record = self._services.store.get_position_lifecycle(position.trade_id)
        if record is not None and record.exit_order_ids:
            return self._adopt_existing_exit_orders(record)
        plan = self._exit_plan(intent, decision, position, snapshots)
        if plan is None:
            return ()
        try:
            submit = self._services.oms.submit_plan(
                plan,
                strategy_id=intent.strategy_id,
                account_id=self._account.config.account_id,
            )
        except OrderFrozenError:
            self._freeze_unknown_exit(position.trade_id)
            return ()
        order_ids = tuple(event.identity.internal_order_id for event in submit.events)
        self._write_lifecycle(position.trade_id, exit_order_ids=order_ids)
        for event in submit.events:
            if event.state is OrderState.UNKNOWN:
                self._freeze_unknown_exit(position.trade_id)
                return submit.events
            self._services.trade_manager.apply_exit_order_event(
                event,
                capital_reservation_id=decision.capital_reservation_id,
            )
        self._write_lifecycle(position.trade_id, exit_order_ids=order_ids)
        closed = self._services.trade_manager.get_position(position.trade_id)
        if closed is not None and closed.state is TradeState.CLOSED:
            self._open_book.pop(position.trade_id, None)
        return submit.events

    def _adopt_existing_exit_orders(
        self, record: PositionLifecycleRecord
    ) -> tuple[OrderEvent, ...]:
        events: list[OrderEvent] = []
        decision = record.risk_decision
        for order_id in record.exit_order_ids:
            event = self._exit_order_event(order_id)
            if event is None or event.state is OrderState.UNKNOWN:
                self._freeze_unknown_exit(record.trade_id)
                return tuple(events)
            events.append(event)
            if event.state in {
                OrderState.FILLED,
                OrderState.REJECTED,
                OrderState.CANCELLED,
                OrderState.EXPIRED,
            }:
                self._services.trade_manager.apply_exit_order_event(
                    event,
                    capital_reservation_id=decision.capital_reservation_id,
                )
        self._write_lifecycle(record.trade_id, exit_order_ids=record.exit_order_ids)
        closed = self._services.trade_manager.get_position(record.trade_id)
        if closed is not None and closed.state is TradeState.CLOSED:
            self._open_book.pop(record.trade_id, None)
        return tuple(events)

    def _exit_order_event(self, order_id: str) -> OrderEvent | None:
        broker_event = self._services.broker.get_order(order_id)
        if broker_event is not None:
            return broker_event
        latest: OrderEvent | None = None
        for stored in self._services.store.read_events():
            if stored.event_type is not TradingEventType.ORDER_EVENT:
                continue
            event = stored.deserialize()
            if not isinstance(event, OrderEvent):
                continue
            if event.identity.internal_order_id != order_id:
                continue
            if latest is None or event.received_at >= latest.received_at:
                latest = event
        return latest

    def _exit_leg_snapshots(
        self,
        position: PositionState,
        intent: TradeIntent,
        snapshots: Mapping[str, FeatureSnapshot],
    ) -> dict[str, FeatureSnapshot] | None:
        mapping: dict[str, FeatureSnapshot] = {}
        required_legs = position.legs
        if position.exit_policy.scope is ExitScope.STRATEGY_PNL:
            position_ids = {leg.leg_id for leg in position.legs}
            if any(leg.leg_id not in position_ids for leg in intent.legs):
                return None
        for leg in required_legs:
            snapshot = snapshots.get(leg.contract.symbol)
            if snapshot is None or snapshot.quality.state.blocks_new_exposure:
                return None
            mapping[leg.leg_id] = snapshot
        return mapping

    def _write_lifecycle(
        self,
        trade_id: str,
        *,
        exit_order_ids: tuple[str, ...] | None = None,
    ) -> None:
        position = self._services.trade_manager.get_position(trade_id)
        book = self._open_book.get(trade_id)
        if position is None or book is None:
            return
        intent, decision = book
        existing = self._services.store.get_position_lifecycle(trade_id)
        ids = (
            exit_order_ids
            if exit_order_ids is not None
            else (existing.exit_order_ids if existing is not None else ())
        )
        record = PositionLifecycleRecord(
            trade_id=trade_id,
            position=position,
            intent=intent,
            risk_decision=decision,
            holding_style=_holding_style(intent),
            exit_order_ids=ids,
            as_of=position.as_of,
        )
        if existing is not None and _lifecycle_unchanged(existing, record):
            return
        self._services.store.upsert_position_lifecycle(
            record,
            event_id=self._ids.new_id("PLC"),
        )

    def _freeze_unknown_exit(self, trade_id: str) -> None:
        self._services.controls.freeze_entries(
            actor="paper-lifecycle",
            scope=f"trade/{trade_id}/exit",
            trigger=Trigger.RECONCILIATION,
            incident_id=self._ids.new_id("INC"),
        )
        self._audit_lifecycle_alerts(
            (
                LifecycleAlert(
                    trade_id=trade_id,
                    reason_code=ReasonCode.RECONCILIATION_UNRESOLVED,
                    detail=(
                        "exit order status is unknown; replacement is blocked "
                        "until reconciliation"
                    ),
                ),
            )
        )

    def _audit_lifecycle_alerts(self, alerts: tuple[LifecycleAlert, ...]) -> None:
        now = self._clock.now_utc()
        for alert in alerts:
            difference = (
                DifferenceClass.MISSING_LOCAL_EVENT
                if "no persisted" in alert.detail
                else DifferenceClass.UNEXPECTED_BROKER_STATE
            )
            event = ReconciliationEvent(
                event_id=self._ids.new_id("REC"),
                scope=f"trade/{alert.trade_id}/lifecycle",
                trigger=ReconciliationTrigger.BOOT,
                expected_local_ref=alert.trade_id,
                difference_class=difference,
                severity=Severity.CRITICAL,
                reason_code=alert.reason_code,
                repair_action=(
                    "operator review; block new PAPER entries until the "
                    "position lifecycle matches broker state"
                ),
                entries_blocked=True,
                detected_at=now,
            )
            self._services.store.append(
                TradingEventType.RECONCILIATION_EVENT,
                event,
                event_id=event.event_id,
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
                open_positions=self._services.trade_manager.list_positions(),
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
        filled = False
        for event in submit.events:
            if event.state not in {OrderState.FILLED, OrderState.PARTIAL}:
                continue
            position = self._services.trade_manager.apply_order_event(
                event,
                intent=intent,
                capital_reservation_id=risk.capital_reservation_id,
            )
            filled = True
            if (
                event.state is OrderState.FILLED
                and (
                    position.state is TradeState.OPENING
                    or position.state.requires_protective_coverage
                )
                and plan.protective_orders
            ):
                self._services.trade_manager.register_protective_orders(
                    trade_id,
                    tuple(stub.stub_id for stub in plan.protective_orders),
                )
            self._open_book[trade_id] = (intent, risk)
        if filled:
            self._write_lifecycle(trade_id)
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


def _holding_style(intent: TradeIntent) -> HoldingStyle:
    if intent.exit_template.time_exit is not None:
        return HoldingStyle.INTRADAY
    return HoldingStyle.POSITIONAL


def _monitor_snapshot(
    intent: TradeIntent, leg_snapshots: Mapping[str, FeatureSnapshot]
) -> FeatureSnapshot | None:
    watched = monitor_leg(intent)
    return leg_snapshots.get(watched.leg_id)


def _lifecycle_unchanged(
    existing: PositionLifecycleRecord, updated: PositionLifecycleRecord
) -> bool:
    comparable = existing.position.model_copy(update={"as_of": updated.position.as_of})
    return (
        comparable == updated.position
        and existing.intent == updated.intent
        and existing.risk_decision == updated.risk_decision
        and existing.holding_style == updated.holding_style
        and existing.exit_order_ids == updated.exit_order_ids
    )


def _lifecycle_issues(
    record: PositionLifecycleRecord,
    broker_legs: tuple[PositionRecord, ...],
) -> tuple[LifecycleAlert, ...]:
    alerts: list[LifecycleAlert] = []
    position = record.position
    if (
        position.state.requires_protective_coverage
        and not position.protective_order_ids
    ):
        alerts.append(
            LifecycleAlert(
                trade_id=record.trade_id,
                reason_code=ReasonCode.PROTECTIVE_COVERAGE_MISSING,
                detail=(
                    "open position has no local protective stub coverage; "
                    "PAPER stops are software-only"
                ),
            )
        )
    if position.state is TradeState.REPAIR_REQUIRED:
        alerts.append(
            LifecycleAlert(
                trade_id=record.trade_id,
                reason_code=ReasonCode.PARTIAL_FILL_UNREPAIRED,
                detail="partial or incomplete multi-leg fill requires repair",
            )
        )
    elif len(position.legs) != len(record.intent.legs):
        alerts.append(
            LifecycleAlert(
                trade_id=record.trade_id,
                reason_code=ReasonCode.PARTIAL_FILL_UNREPAIRED,
                detail="persisted legs do not match the frozen intent structure",
            )
        )
    local_keys = {
        (leg.contract.symbol, leg.side, leg.quantity_contracts) for leg in position.legs
    }
    broker_keys = {
        (leg.contract.symbol, leg.side, leg.quantity_contracts) for leg in broker_legs
    }
    if local_keys != broker_keys:
        alerts.append(
            LifecycleAlert(
                trade_id=record.trade_id,
                reason_code=ReasonCode.RECONCILIATION_UNRESOLVED,
                detail="persisted position legs do not match PAPER broker state",
            )
        )
    return tuple(alerts)


def _unique_alerts(alerts: list[LifecycleAlert]) -> tuple[LifecycleAlert, ...]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[LifecycleAlert] = []
    for alert in alerts:
        key = (alert.trade_id, alert.reason_code.value, alert.detail)
        if key in seen:
            continue
        seen.add(key)
        unique.append(alert)
    return tuple(unique)
