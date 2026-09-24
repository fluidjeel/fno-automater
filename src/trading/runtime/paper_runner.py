"""Supervised PAPER cycle: live snapshots in, paper broker out.

Invariant 2: this process never constructs a live transaction adapter.
Invariant 22: it does not write live configuration.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading.ai.decision_log import DecisionLog
from trading.ai.entry import maybe_log_entry_shadow
from trading.ai.packets import build_delta_packet
from trading.ai.position import maybe_log_position_shadow
from trading.broker.paper import PaperBroker
from trading.config.charge_policy import LoadedChargePolicy, load_charge_policy
from trading.config.evaluation import FillModelConfig
from trading.config.loader import LoadedConfig
from trading.config.risk_policy import LoadedRiskPolicy, MissingMonitorResolution
from trading.data.cas_features import with_cas_feature_set
from trading.domain.clock import Clock
from trading.domain.contracts import (
    EntryFreezeRecord,
    FeatureSnapshot,
    InstrumentSpec,
    MarketState,
    OrderEvent,
    PositionCarryRecord,
    PositionLifecycleRecord,
    PositionReviewRecord,
    ReconciliationEvent,
    RiskDecision,
    RollSwitchTransition,
    RouteDecision,
    SetupFeatures,
    TradeIntent,
)
from trading.domain.contracts.carry import CarryGateDecision
from trading.domain.contracts.common import Versions
from trading.domain.contracts.entry import StrikeShortlist
from trading.domain.contracts.order import OrderCommand, OrderIdentity
from trading.domain.contracts.order_plan import OrderPlan, PlannedOrder
from trading.domain.contracts.paper_data import PaperDataField, PaperDataRequirements
from trading.domain.contracts.portfolio import PositionRecord
from trading.domain.contracts.position import PositionState
from trading.domain.contracts.protection import ProtectionStateRecord
from trading.domain.contracts.snapshot import MarketQuote, SnapshotTimes
from trading.domain.enums import (
    CarryGateAction,
    DeskRole,
    DifferenceClass,
    Exchange,
    ExecutionMode,
    ExitScope,
    FamilyId,
    HoldingStyle,
    ModeId,
    OrderPlanState,
    OrderState,
    OrderType,
    ProtectionStatus,
    ReasonCode,
    ReconciliationTrigger,
    ReviewAction,
    ReviewExecutionStatus,
    ReviewSlotId,
    RiskAction,
    RollSwitchStatus,
    Severity,
    Side,
    SystemState,
    TimeInForce,
    TradeState,
    Trigger,
)
from trading.domain.ids import IdFactory, derive_idempotency_key
from trading.domain.primitives import Currency, Money
from trading.identification.p1_features import observe_exit_depth
from trading.news.contracts import EventRiskState
from trading.oms import (
    OmsEngine,
    OrderFrozenError,
    OrderPlanPlanner,
    OrderPlanRequest,
    OrderRateLimiter,
)
from trading.portfolio import (
    ArbitrationResult,
    PortfolioArbiter,
    PortfolioReconciler,
    build_broker_snapshot,
    build_portfolio_view,
)
from trading.portfolio.campaign_drawdown import (
    CampaignLedger,
    campaign_loss_limit,
    trade_accounting,
)
from trading.portfolio.fill_charge_recorder import (
    backfill_fill_charges,
    record_fill_charge,
)
from trading.portfolio.fill_ledger import index_fill_charges, index_order_events
from trading.risk import CapitalReservationService, RiskGateway, RiskGatewayRequest
from trading.risk.mode_ledger import FourModeBook
from trading.runtime.cycle_evidence import build_cycle_evidence
from trading.runtime.isolation import assert_paper_isolation
from trading.runtime.review_schedule import ReviewSlot
from trading.safety import ReadinessEvaluator, ReadinessRequest, SafetyControls
from trading.safety.paper_data import PaperDataInputs, assess_paper_data
from trading.storage.trading_store import TradingEventType, TradingStore
from trading.strategies import StrategyContext, build_strategy
from trading.strategies.macro import MacroAssessment
from trading.trade import (
    TradeManager,
    assert_stop_not_wider,
    monitor_leg,
)
from trading.trade.carry_gate import (
    build_m2_carry_gate_input,
    evaluate_m2_carry_gate,
    load_carry_gate_config,
    resolve_carry_market,
)
from trading.trade.exits import ExitEvaluation, ExitKind
from trading.trade.review import ReviewEngine, ReviewEvaluation, structure_exit_quantity
from trading.trade.roll_switch import (
    begin_roll_switch_transition,
    replacement_blocked_reason,
    resolve_roll_switch_review,
    transition_after_close,
)

logger = logging.getLogger(__name__)

__all__ = [
    "LifecycleAlert",
    "PaperCarryGateResult",
    "PaperCycleResult",
    "PaperReviewResult",
    "PaperRollSwitchReplacementResult",
    "PaperRunner",
    "PaperStrategyOutcome",
    "PaperStrategyRequest",
    "PositionRecoveryResult",
    "QuoteUpdateResult",
]


@dataclass(frozen=True, slots=True)
class LifecycleAlert:
    """One unreconciled or unprotected open-position finding."""

    trade_id: str
    reason_code: ReasonCode
    detail: str


def _liability_first(orders: Sequence[PlannedOrder]) -> tuple[PlannedOrder, ...]:
    """Close short liabilities before selling the longs that cover them.

    A structure exit is executed as separate orders, so every prefix of the
    sequence is a position the account can be left holding if a later order is
    delayed, rejected, unknown or partially filled. Buying back a short first
    keeps each prefix covered; selling the protective long first would leave a
    transient uncovered short. Order within each group is preserved so replay
    stays deterministic.
    """
    covering = [order for order in orders if order.command.side is Side.BUY]
    releasing = [order for order in orders if order.command.side is not Side.BUY]
    return (*covering, *releasing)


@dataclass(frozen=True, slots=True)
class QuoteUpdateResult:
    """Outcome of one event-driven protection quote evaluation."""

    alerts: tuple[LifecycleAlert, ...] = ()
    events: tuple[OrderEvent, ...] = ()
    degraded: bool = False
    recovered: bool = False
    detection_latency_ms: int | None = None


@dataclass(frozen=True, slots=True)
class PositionRecoveryResult:
    """Outcome of restoring persisted PAPER position lifecycles."""

    restored_trade_ids: tuple[str, ...]
    entries_blocked: bool
    alerts: tuple[LifecycleAlert, ...]
    unprotected_trade_ids: tuple[str, ...]
    unreconciled_trade_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PaperRollSwitchReplacementResult:
    """Outcome of one roll/switch replacement attempt after structure close."""

    approved: bool
    reason_codes: tuple[ReasonCode, ...]
    replacement_trade_id: str | None
    transition: RollSwitchTransition | None


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
    shortlist: StrikeShortlist | None = None
    forced_mode_id: ModeId | None = None
    forced_family_id: FamilyId | None = None
    campaign_id: str | None = None


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
    arbitration_result: ArbitrationResult | None = None


@dataclass(frozen=True, slots=True)
class PaperReviewResult:
    """Outcome of one scheduled positional review slot."""

    slot_id: ReviewSlotId
    session_date: date
    decisions: tuple[PositionReviewRecord, ...]
    slot_recorded: bool
    missed_slot_ids: tuple[ReviewSlotId, ...] = ()
    is_recovery: bool = False


@dataclass(frozen=True, slots=True)
class PaperCarryGateResult:
    """Outcome of one Mode 2 carry gate pass for a session date."""

    session_date: date
    decisions: tuple[PositionCarryRecord, ...]
    exit_events: tuple[OrderEvent, ...]


class _ProtectionKind:
    OK = "OK"
    MISSING_MONITOR = "MISSING_MONITOR"
    STALE = "STALE"


@dataclass(frozen=True, slots=True)
class _ProtectionDiagnosis:
    kind: str
    reason_code: ReasonCode
    detail: str


@dataclass(slots=True)
class _StrategyEvalRecord:
    request: PaperStrategyRequest
    early_outcome: PaperStrategyOutcome | None = None
    intents: tuple[TradeIntent, ...] = ()
    rejection_reasons: tuple[ReasonCode, ...] = ()
    strategy: object | None = None
    portfolio: object | None = None
    can_execute: bool = False


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
        charge_policy: LoadedChargePolicy | None = None,
        execution_mode: ExecutionMode = ExecutionMode.PAPER,
        paper_data_requirements: PaperDataRequirements | None = None,
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
        self._charge_policy = charge_policy or load_charge_policy()
        self._clock = clock
        self._ids = id_factory
        self._execution_mode = execution_mode
        self._paper_data = paper_data_requirements
        self._exit_depth_gaps: tuple[str, ...] = ()
        self._readiness = ReadinessEvaluator()
        reservations = CapitalReservationService(
            store, clock=clock, id_factory=id_factory
        )
        session_date = clock.now_utc().astimezone(ZoneInfo("Asia/Kolkata")).date()
        mode_book = FourModeBook.reconstruct_from_store(store, session_date)
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
                mode_book=mode_book,
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
        self._arbiter = PortfolioArbiter()
        self._open_book: dict[str, tuple[TradeIntent, RiskDecision]] = {}
        self._protection_snapshots: dict[str, FeatureSnapshot] = {}
        self._lifecycle_recovered = False
        self._decision_log = DecisionLog(store)
        self._review_engine = ReviewEngine()
        self._last_recovery = PositionRecoveryResult(
            restored_trade_ids=(),
            entries_blocked=False,
            alerts=(),
            unprotected_trade_ids=(),
            unreconciled_trade_ids=(),
        )
        self._campaign_ledger = CampaignLedger()
        self._trade_campaign: dict[str, str] = {}
        self._campaign_close_recorded: set[str] = set()

    def run_cycle(self, requests: Sequence[PaperStrategyRequest]) -> PaperCycleResult:
        """Reconcile, evaluate each strategy, arbitrate, and submit approved orders."""
        self.recover_lifecycle()
        boot = self._services.reconciler.boot_reconcile(self._account.config.account_id)
        system_state = boot.result.resulting_system_state
        self._maybe_release_entry_freeze(reconcile_blocked=boot.result.entries_blocked)
        entries_blocked = self._entries_are_blocked(boot.result.entries_blocked)

        eval_records: list[_StrategyEvalRecord] = []
        all_executable_intents: list[TradeIntent] = []
        for request in requests:
            assert_paper_isolation(
                self._account.config.environment,
                request.execution_mode,
                self._services.broker,
            )
            rec = self._evaluate_strategy(
                request,
                system_state=system_state,
                entries_blocked=entries_blocked,
                reconcile_id=boot.result.result_id,
            )
            eval_records.append(rec)
            if rec.can_execute:
                all_executable_intents.extend(rec.intents)

        arb_result = self._arbiter.arbitrate(
            all_executable_intents,
            existing_positions=self._services.trade_manager.list_positions(),
            now=self._clock.now_utc(),
        )

        outcomes: list[PaperStrategyOutcome] = []
        for rec in eval_records:
            outcomes.append(
                self._execute_strategy_eval(
                    rec,
                    arb_result=arb_result,
                    entries_blocked=entries_blocked,
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
            arbitration_result=arb_result,
        )

    def persist_cycle_evidence(
        self, result: PaperCycleResult, *, as_of: datetime
    ) -> int:
        """Append one durable cycle-evidence event for dashboard traceability."""
        cycle_id = self._ids.new_id("CYC")
        evidence = build_cycle_evidence(result, cycle_id=cycle_id, as_of=as_of)
        return self._services.store.append(
            TradingEventType.CYCLE_EVIDENCE,
            evidence,
            event_id=cycle_id,
            recorded_at=as_of,
        )

    @property
    def broker(self) -> PaperBroker:
        return self._services.broker

    @property
    def id_factory(self) -> IdFactory:
        return self._ids

    @property
    def trade_manager(self) -> TradeManager:
        return self._services.trade_manager

    @property
    def protection_degraded(self) -> bool:
        return self._services.controls.state.protection_degraded or any(
            position.protection_degraded or position.software_stop_unavailable
            for position in self._services.trade_manager.list_positions()
            if position.state is not TradeState.CLOSED
        )

    def open_position_count(self) -> int:
        return sum(
            position.state is not TradeState.CLOSED
            for position in self._services.trade_manager.list_positions()
        )

    def monitor_symbols(self) -> tuple[str, ...]:
        symbols: set[str] = set()
        for trade_id, (intent, _) in self._open_book.items():
            if any(
                p.trade_id == trade_id and p.state is not TradeState.CLOSED
                for p in self._services.trade_manager.list_positions()
            ):
                symbols.update(leg.contract.symbol for leg in intent.legs)
        return tuple(sorted(symbols))

    def seed_protection_snapshots(
        self, snapshots: Mapping[str, FeatureSnapshot]
    ) -> None:
        self._protection_snapshots.update(snapshots)

    def persist_session_protection(self, state: object) -> None:
        self._services.store.upsert_session_protection(state)  # type: ignore[arg-type]

    def on_quote_update(
        self,
        quotes: Mapping[str, MarketQuote],
        *,
        source: object,
        received_at: datetime,
        quote_max_age_ms: int,
    ) -> QuoteUpdateResult:
        before = self.protection_degraded
        updated: dict[str, FeatureSnapshot] = {}
        for symbol, quote in quotes.items():
            snapshot = self._protection_snapshots.get(symbol)
            if snapshot is None:
                continue
            updated[symbol] = snapshot.model_copy(
                update={
                    "market": quote,
                    "times": SnapshotTimes(
                        event_time=received_at,
                        source_time=received_at,
                        receive_time=received_at,
                        calculation_time=received_at,
                    ),
                }
            )
        self._protection_snapshots.update(updated)
        events = self.manage_exits(self._protection_snapshots)
        if updated and all(
            not snapshot.quality.state.blocks_new_exposure
            for snapshot in updated.values()
        ):
            for position in self._services.trade_manager.list_positions():
                if position.state is TradeState.OPEN:
                    self._clear_protection_degraded(position)
            self._services.controls.restore_protection(
                actor="paper-protection", scope="session"
            )
        latency = None
        if updated:
            latency = max(
                0,
                *(
                    int((received_at - snap.times.event_time).total_seconds() * 1000)
                    for snap in updated.values()
                ),
            )
        return QuoteUpdateResult(
            events=events,
            degraded=self.protection_degraded,
            recovered=before and not self.protection_degraded,
            detection_latency_ms=latency,
        )

    @property
    def open_book(self) -> Mapping[str, tuple[TradeIntent, RiskDecision]]:
        return self._open_book

    @property
    def last_recovery(self) -> PositionRecoveryResult:
        return self._last_recovery

    @property
    def exit_depth_gaps(self) -> tuple[str, ...]:
        """Symbols whose exit snapshots lacked observed depth this cycle."""
        return self._exit_depth_gaps

    def recover_lifecycle(self) -> PositionRecoveryResult:
        """Restore persisted positions and block entries on unresolved gaps.

        Invariant 9: restart begins in recovery; new entries wait. Protective
        stub IDs are local software coverage, not broker-resident PAPER stops.
        """
        if self._lifecycle_recovered:
            return self._last_recovery
        self._restore_persisted_freeze()
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
            protection = self._services.store.get_protection_state(persisted.trade_id)
            if (
                protection is not None
                and protection.status is ProtectionStatus.DEGRADED
            ):
                self._services.controls.degrade_protection(
                    actor="paper-recovery", scope=f"trade/{persisted.trade_id}"
                )
                unprotected.append(persisted.trade_id)
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
                            reason_code=ReasonCode.UNKNOWN_ORDER_STATUS,
                            detail=(
                                "exit order status is unknown; replacement is "
                                "blocked until reconciliation"
                            ),
                        )
                    )
                    unreconciled.append(persisted.trade_id)
                    break
            if persisted.position.state is TradeState.EXIT_PENDING and not any(
                alert.trade_id == persisted.trade_id
                and alert.reason_code is ReasonCode.UNKNOWN_ORDER_STATUS
                for alert in alerts
            ):
                alerts.append(
                    LifecycleAlert(
                        trade_id=persisted.trade_id,
                        reason_code=ReasonCode.UNKNOWN_ORDER_STATUS,
                        detail=(
                            "trade is EXIT_PENDING; new entries stay blocked until "
                            "the in-flight exit reconciles"
                        ),
                    )
                )
                unreconciled.append(persisted.trade_id)
            if persisted.position.protection_degraded:
                alerts.append(
                    LifecycleAlert(
                        trade_id=persisted.trade_id,
                        reason_code=ReasonCode.PROTECTION_DEGRADED,
                        detail=(
                            persisted.position.unprotected_reason
                            or (
                                "persisted PROTECTION_DEGRADED; PAPER has no "
                                "broker-resident stop"
                            )
                        ),
                    )
                )
        for trade_id in sorted(set(broker_by_trade) - active_ids):
            alerts.append(
                LifecycleAlert(
                    trade_id=trade_id,
                    reason_code=ReasonCode.UNRECONCILED_POSITION,
                    detail="broker position has no persisted exit lifecycle",
                )
            )
            unreconciled.append(trade_id)
        unique_alerts = tuple(_unique_alerts(alerts))
        persisted_freeze = self._services.store.get_entry_freeze()
        entries_blocked = bool(unique_alerts or unprotected or unreconciled) or (
            persisted_freeze is not None and persisted_freeze.entries_blocked
        )
        if unique_alerts:
            primary = unique_alerts[0]
            self._ensure_entries_blocked(primary.reason_code, primary.detail)
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
        self._backfill_fill_charges()
        self._restore_campaign_ledger()
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
        gaps: list[str] = []
        events.extend(self._resume_inflight_exits(snapshots))
        for position in self._services.trade_manager.list_positions():
            if position.state is not TradeState.OPEN:
                continue
            book = self._open_book.get(position.trade_id)
            if book is None:
                continue
            intent, decision = book
            diagnosis = self._diagnose_protection(position, intent, snapshots)
            if diagnosis.kind is _ProtectionKind.MISSING_MONITOR:
                events.extend(
                    self._handle_missing_monitor(
                        position, intent, decision, snapshots, diagnosis
                    )
                )
                continue
            if diagnosis.kind is _ProtectionKind.STALE:
                self._handle_stale_protection(position, diagnosis)
                continue
            self._clear_protection_degraded(position)
            leg_snapshots = self._exit_leg_snapshots(position, intent, snapshots)
            if leg_snapshots is None:
                continue
            feature = _monitor_snapshot(intent, leg_snapshots)
            if feature is None:
                continue
            self._note_exit_depth(feature, leg_snapshots, gaps)
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
        self._exit_depth_gaps = tuple(dict.fromkeys(gaps))
        return tuple(events)

    def run_review_slot(
        self,
        slot: ReviewSlot,
        snapshots: Mapping[str, FeatureSnapshot],
        *,
        session_date: date,
        missed_slot_ids: tuple[ReviewSlotId, ...] = (),
        configured_slots: tuple[ReviewSlot, ...] = (),
    ) -> PaperReviewResult:
        """Review persisted positional opens for one due slot. Idempotent.

        HEDGE/ROLL/SWITCH proposals submit only for G2 close/open families.
        Stops stay software-only; this does not place a broker-resident stop.
        """
        recovery_ids = missed_slot_ids or (slot.slot_id,)
        if all(
            self._services.store.has_review_slot_run(slot_id, session_date)
            for slot_id in recovery_ids
        ):
            return PaperReviewResult(
                slot_id=slot.slot_id,
                session_date=session_date,
                decisions=(),
                slot_recorded=False,
                missed_slot_ids=missed_slot_ids,
                is_recovery=len(missed_slot_ids) > 1,
            )
        decisions = self._review_positions(
            slot,
            snapshots,
            session_date=session_date,
            missed_slot_ids=missed_slot_ids,
            configured_slots=configured_slots,
        )
        inserted = False
        now = self._clock.now_utc()
        for slot_id in recovery_ids:
            if self._services.store.record_review_slot_run(
                slot_id=slot_id,
                session_date=session_date,
                venue=slot.venue,
                as_of=now,
            ):
                inserted = True
        return PaperReviewResult(
            slot_id=slot.slot_id,
            session_date=session_date,
            decisions=decisions,
            slot_recorded=inserted,
            missed_slot_ids=missed_slot_ids,
            is_recovery=len(missed_slot_ids) > 1,
        )

    def run_m2_carry_gate(
        self,
        snapshots: Mapping[str, FeatureSnapshot],
        *,
        session_date: date,
        market: MarketState | None,
        mode_reference_capital: Money,
        event_blackout: bool,
        portfolio_entries_blocked: bool,
        mode_daily_loss_breached: bool,
    ) -> PaperCarryGateResult:
        """Evaluate Mode 2 overnight carry once per trade per session date."""
        now = self._clock.now_utc()
        config = load_carry_gate_config()
        decisions: list[PositionCarryRecord] = []
        exit_events: list[OrderEvent] = []
        for position in self._services.trade_manager.list_positions():
            if position.state is not TradeState.OPEN:
                continue
            mode_id = position.mode_id
            book = self._open_book.get(position.trade_id)
            if book is None:
                continue
            intent, decision = book
            mode_id = mode_id or intent.mode_id
            if mode_id is not ModeId.M2_DIRECTIONAL:
                continue
            persisted = self._services.store.get_position_lifecycle(position.trade_id)
            prior = persisted.carry_records if persisted is not None else ()
            if any(item.session_date == session_date for item in prior):
                continue
            remaining_dte = _remaining_dte(position, intent, snapshots)
            if remaining_dte is None:
                continue
            recovery_healthy = not (
                position.protection_degraded or position.software_stop_unavailable
            )
            carry_market = market or resolve_carry_market(
                intent,
                session_date=session_date,
                as_of=now,
                cached=market,
            )
            inputs = build_m2_carry_gate_input(
                position=position,
                intent=intent,
                market=carry_market,
                session_date=session_date,
                mode_reference_capital=mode_reference_capital,
                config=config,
                event_blackout=event_blackout,
                portfolio_entries_blocked=portfolio_entries_blocked,
                mode_daily_loss_breached=mode_daily_loss_breached,
                recovery_healthy=recovery_healthy,
                remaining_dte=remaining_dte,
            )
            outcome = evaluate_m2_carry_gate(
                trade_id=position.trade_id,
                session_date=session_date,
                inputs=inputs,
                config=config,
                as_of=now,
                decision_id=self._ids.new_id("CRG"),
            )
            record = _stamp_carry(outcome, carry_id=self._ids.new_id("CRR"), now=now)
            if outcome.action is CarryGateAction.CARRY_APPROVED:
                self._services.trade_manager.clear_intraday_time_exit(
                    position.trade_id, now=now
                )
                carried = intent.model_copy(
                    update={
                        "mode_id": ModeId.M2_DIRECTIONAL,
                        "exit_template": intent.exit_template.model_copy(
                            update={"time_exit": None}
                        ),
                    }
                )
                self._open_book[position.trade_id] = (carried, decision)
            if outcome.exit_initiated:
                exit_events.extend(
                    self._initiate_carry_rejection_exit(
                        position,
                        intent=intent,
                        decision=decision,
                        snapshots=snapshots,
                        detail=outcome.detail,
                    )
                )
            self._write_lifecycle(position.trade_id, extra_carry=(record,))
            decisions.append(record)
        return PaperCarryGateResult(
            session_date=session_date,
            decisions=tuple(decisions),
            exit_events=tuple(exit_events),
        )

    def recorded_review_slots(
        self, session_date: date
    ) -> frozenset[tuple[date, ReviewSlotId]]:
        """Slots already persisted for an IST session date."""
        return frozenset(
            (day, slot_id)
            for slot_id, day in self._services.store.list_review_slot_runs(session_date)
        )

    def _review_positions(
        self,
        slot: ReviewSlot,
        snapshots: Mapping[str, FeatureSnapshot],
        *,
        session_date: date,
        missed_slot_ids: tuple[ReviewSlotId, ...] = (),
        configured_slots: tuple[ReviewSlot, ...] = (),
    ) -> tuple[PositionReviewRecord, ...]:
        now = self._clock.now_utc()
        decisions: list[PositionReviewRecord] = []
        for position in self._services.trade_manager.list_positions():
            book = self._open_book.get(position.trade_id)
            if book is None:
                continue
            intent, decision = book
            holding = _holding_style(intent)
            if holding is not HoldingStyle.POSITIONAL:
                continue
            persisted = self._services.store.get_position_lifecycle(position.trade_id)
            if not _eligible_for_scheduled_review(position, intent, persisted):
                continue
            if not _matches_slot_venue(position, slot.venue):
                continue
            prior = persisted.reviews if persisted is not None else ()
            if _review_already_recorded(
                prior,
                slot_id=slot.slot_id,
                session_date=session_date,
                missed_slot_ids=missed_slot_ids,
            ):
                continue
            diagnosis = self._diagnose_protection(position, intent, snapshots)
            if diagnosis.kind != _ProtectionKind.OK:
                if diagnosis.kind == _ProtectionKind.MISSING_MONITOR:
                    self._handle_missing_monitor(
                        position, intent, decision, snapshots, diagnosis
                    )
                else:
                    self._handle_stale_protection(position, diagnosis)
                review = _unavailable_review(
                    trade_id=position.trade_id,
                    policy_id=position.exit_policy.policy_id,
                    slot_id=slot.slot_id,
                    session_date=session_date,
                    review_id=self._ids.new_id("REV"),
                    now=now,
                    reason_code=diagnosis.reason_code,
                    detail=diagnosis.detail,
                )
                self._write_lifecycle(position.trade_id, extra_reviews=(review,))
                decisions.append(review)
                continue
            self._clear_protection_degraded(position)
            leg_snapshots = self._exit_leg_snapshots(position, intent, snapshots)
            feature = (
                _monitor_snapshot(intent, leg_snapshots)
                if leg_snapshots is not None
                else None
            )
            if feature is None:
                review = _unavailable_review(
                    trade_id=position.trade_id,
                    policy_id=position.exit_policy.policy_id,
                    slot_id=slot.slot_id,
                    session_date=session_date,
                    review_id=self._ids.new_id("REV"),
                    now=now,
                    reason_code=ReasonCode.UNPROTECTED_POSITION,
                    detail=(
                        "monitor leg quote missing; software stop cannot evaluate "
                        "and PAPER has no broker-resident stop"
                    ),
                )
                self._write_lifecycle(position.trade_id, extra_reviews=(review,))
                decisions.append(review)
                continue
            evaluation = self._review_engine.evaluate(
                position,
                intent,
                feature,
                now=now,
                slot_id=slot.slot_id,
                session_date=session_date,
                holding_style=holding,
                prior_reviews=prior,
                leg_snapshots=leg_snapshots,
            )
            if evaluation.reason_code is ReasonCode.REVIEW_DUPLICATE_SLOT:
                continue

            evaluation, execution_status, next_slot_id = resolve_roll_switch_review(
                evaluation,
                intent=intent,
                position=position,
                slot=slot,
                configured_slots=configured_slots,
            )

            review_id = self._ids.new_id("REV")
            spot_price = Decimal("0")
            if (
                feature.derivatives is not None
                and feature.derivatives.underlying_price is not None
            ):
                spot_price = feature.derivatives.underlying_price.value
            packet = build_delta_packet(
                role=DeskRole.POSITION,
                as_of=now,
                snapshot_id=feature.snapshot_id,
                trade_id=position.trade_id,
                spot=spot_price,
                unrealized_r=Decimal("0"),
                question=f"Review slot {slot.slot_id.value} action",
            )
            # Deterministic exit/tighten must not wait on AI/shadow. Shadow is
            # advisory-only and any timeout/failure is logged without blocking.
            submitted = self._apply_review(
                evaluation,
                intent=intent,
                decision=decision,
                position=position,
                snapshots=snapshots,
                review_id=review_id,
            )
            try:
                maybe_log_position_shadow(
                    packet,
                    slot_id=slot.slot_id,
                    deterministic_action=evaluation.action,
                    decision_log=self._decision_log,
                    enabled=True,
                    run_id=f"RUN-REV-{slot.slot_id.value}",
                    environment=self._account.config.environment,
                    now=now,
                )
            except Exception:
                logger.exception(
                    "position shadow logging failed after deterministic review "
                    "trade_id=%s slot=%s",
                    position.trade_id,
                    slot.slot_id.value,
                )
            review = _stamp_review(
                evaluation,
                trade_id=position.trade_id,
                policy_id=position.exit_policy.policy_id,
                slot_id=slot.slot_id,
                session_date=session_date,
                review_id=review_id,
                submitted=submitted,
                now=now,
                execution_status=execution_status,
                missed_slot_ids=missed_slot_ids,
                next_slot_id=next_slot_id,
            )
            self._write_lifecycle(position.trade_id, extra_reviews=(review,))
            decisions.append(review)
        return tuple(decisions)

    def _apply_review(
        self,
        evaluation: ReviewEvaluation,
        *,
        intent: TradeIntent,
        decision: RiskDecision,
        position: PositionState,
        snapshots: Mapping[str, FeatureSnapshot],
        review_id: str,
    ) -> bool:
        if evaluation.action is ReviewAction.TIGHTEN_STOP:
            policy = evaluation.updated_policy
            if policy is None:
                return False
            assert_stop_not_wider(
                position.exit_policy, policy, monitor_leg(intent).side
            )
            self._services.trade_manager.apply_exit_evaluation(
                position.trade_id,
                ExitEvaluation(
                    kind=ExitKind.NONE,
                    reason_code=ReasonCode.OK,
                    detail=evaluation.detail,
                    updated_policy=policy,
                ),
            )
            return False
        if evaluation.submit_structure_close and evaluation.roll_switch_kind is not None:
            return self._submit_roll_switch_close(
                evaluation,
                intent=intent,
                decision=decision,
                position=position,
                snapshots=snapshots,
                review_id=review_id,
            )
        if evaluation.action.is_proposal or evaluation.action is ReviewAction.HOLD:
            return False
        if not evaluation.should_submit_exit:
            return False
        self._services.trade_manager.apply_exit_evaluation(
            position.trade_id,
            ExitEvaluation(
                kind=ExitKind.STOP,
                reason_code=ReasonCode.OK,
                detail=evaluation.detail,
                updated_policy=evaluation.updated_policy,
            ),
        )
        pending = self._services.trade_manager.get_position(position.trade_id)
        if pending is None:
            return False
        events = self._submit_exit(
            intent,
            decision,
            pending,
            snapshots,
            quantity_contracts=structure_exit_quantity(
                position, evaluation.exit_quantity_contracts
            ),
            remaining_stays_open=evaluation.action is ReviewAction.PARTIAL_EXIT,
        )
        return bool(events)

    def _submit_roll_switch_close(
        self,
        evaluation: ReviewEvaluation,
        *,
        intent: TradeIntent,
        decision: RiskDecision,
        position: PositionState,
        snapshots: Mapping[str, FeatureSnapshot],
        review_id: str,
    ) -> bool:
        """Submit the structure close leg of a roll/switch without relabeling it."""
        kind = evaluation.roll_switch_kind
        if kind is None:
            return False
        now = self._clock.now_utc()
        transition = begin_roll_switch_transition(
            kind=kind,
            review_id=review_id,
            as_of=now,
        )
        self._write_lifecycle(
            position.trade_id, roll_switch_transition=transition
        )
        self._services.trade_manager.apply_exit_evaluation(
            position.trade_id,
            ExitEvaluation(
                kind=ExitKind.STOP,
                reason_code=ReasonCode.OK,
                detail=evaluation.detail,
                updated_policy=evaluation.updated_policy,
            ),
        )
        pending = self._services.trade_manager.get_position(position.trade_id)
        if pending is None:
            return False
        events = self._submit_exit(
            intent,
            decision,
            pending,
            snapshots,
            quantity_contracts=None,
        )
        return bool(events)

    def submit_roll_switch_replacement(
        self,
        trade_id: str,
        request: PaperStrategyRequest,
    ) -> PaperRollSwitchReplacementResult:
        """Submit a replacement leg after close; requires fresh Layer 2 approval."""
        lifecycle = self._services.store.get_position_lifecycle(trade_id)
        transition = (
            None if lifecycle is None else lifecycle.roll_switch_transition
        )
        blocked = replacement_blocked_reason(
            transition,
            entries_blocked=self._entries_are_blocked(reconcile_blocked=False),
        )
        if blocked is not None and blocked is not ReasonCode.OK:
            return PaperRollSwitchReplacementResult(
                approved=False,
                reason_codes=(blocked,),
                replacement_trade_id=None,
                transition=transition,
            )
        position = self._services.trade_manager.get_position(trade_id)
        closed = (
            position is not None and position.state is TradeState.CLOSED
        ) or (
            lifecycle is not None and lifecycle.position.state is TradeState.CLOSED
        )
        if not closed:
            return PaperRollSwitchReplacementResult(
                approved=False,
                reason_codes=(ReasonCode.UNRECONCILED_POSITION,),
                replacement_trade_id=None,
                transition=transition,
            )
        if transition is None or lifecycle is None:
            return PaperRollSwitchReplacementResult(
                approved=False,
                reason_codes=(ReasonCode.INSTRUMENT_UNKNOWN,),
                replacement_trade_id=None,
                transition=transition,
            )
        if not request.execute:
            return self._reject_roll_switch_replacement(
                trade_id,
                transition=transition,
                reason_codes=(ReasonCode.CAPITAL_UNAVAILABLE,),
            )
        pending_transition = transition.model_copy(
            update={
                "status": RollSwitchStatus.REPLACEMENT_PENDING_L2,
                "as_of": self._clock.now_utc(),
            }
        )
        self._write_lifecycle(
            trade_id, roll_switch_transition=pending_transition
        )
        stamped = _stamp_roll_switch_replacement_request(request, pending_transition)
        if lifecycle.campaign_id is not None:
            stamped = PaperStrategyRequest(
                strategy_id=stamped.strategy_id,
                underlying=stamped.underlying,
                candidates=stamped.candidates,
                instruments=stamped.instruments,
                event_risk_state=stamped.event_risk_state,
                experiment_id=stamped.experiment_id,
                execution_mode=stamped.execution_mode,
                macro=stamped.macro,
                execute=stamped.execute,
                setup_features=stamped.setup_features,
                route_decision=stamped.route_decision,
                shortlist=stamped.shortlist,
                forced_mode_id=stamped.forced_mode_id,
                forced_family_id=stamped.forced_family_id,
                campaign_id=lifecycle.campaign_id,
            )
        result = self.run_cycle((stamped,))
        if not result.outcomes:
            return self._reject_roll_switch_replacement(
                trade_id, transition=pending_transition
            )
        outcome = result.outcomes[0]
        if outcome.order_events and not outcome.rejection_reasons:
            replacement_trade_id = outcome.order_events[0].identity.trade_id
            completed = pending_transition.model_copy(
                update={
                    "status": RollSwitchStatus.COMPLETE,
                    "replacement_trade_id": replacement_trade_id,
                    "as_of": self._clock.now_utc(),
                }
            )
            if lifecycle.campaign_id is not None:
                self._link_campaign_trade(
                    lifecycle.campaign_id, replacement_trade_id
                )
            self._write_lifecycle(
                trade_id,
                roll_switch_transition=completed,
                patch_last_review_execution=ReviewExecutionStatus.ROLL_SWITCH_COMPLETE,
                completed_roll_switch=transition.kind,
            )
            return PaperRollSwitchReplacementResult(
                approved=True,
                reason_codes=(ReasonCode.OK,),
                replacement_trade_id=replacement_trade_id,
                transition=completed,
            )
        reasons = outcome.rejection_reasons or outcome.entry_blocked_reasons
        return self._reject_roll_switch_replacement(
            trade_id,
            transition=pending_transition,
            reason_codes=reasons or (ReasonCode.CAPITAL_UNAVAILABLE,),
        )

    def _reject_roll_switch_replacement(
        self,
        trade_id: str,
        *,
        transition: RollSwitchTransition,
        reason_codes: tuple[ReasonCode, ...] = (ReasonCode.CAPITAL_UNAVAILABLE,),
    ) -> PaperRollSwitchReplacementResult:
        rejected = transition.model_copy(
            update={
                "status": RollSwitchStatus.REPLACEMENT_REJECTED,
                "rejection_reason": reason_codes[0],
                "as_of": self._clock.now_utc(),
            }
        )
        self._write_lifecycle(
            trade_id,
            roll_switch_transition=rejected,
            patch_last_review_execution=ReviewExecutionStatus.REPLACEMENT_REJECTED,
        )
        return PaperRollSwitchReplacementResult(
            approved=False,
            reason_codes=reason_codes,
            replacement_trade_id=None,
            transition=rejected,
        )

    def _initiate_carry_rejection_exit(
        self,
        position: PositionState,
        *,
        intent: TradeIntent,
        decision: RiskDecision,
        snapshots: Mapping[str, FeatureSnapshot],
        detail: str,
    ) -> tuple[OrderEvent, ...]:
        """Start an orderly exit when carry is rejected while the session is tradable."""
        self._services.trade_manager.apply_exit_evaluation(
            position.trade_id,
            ExitEvaluation(
                kind=ExitKind.STOP,
                reason_code=ReasonCode.OK,
                detail=f"carry rejected: {detail}",
                updated_policy=position.exit_policy,
            ),
        )
        pending = self._services.trade_manager.get_position(position.trade_id)
        if pending is None:
            return ()
        return self._submit_exit(intent, decision, pending, snapshots)

    def _exit_plan(
        self,
        intent: TradeIntent,
        decision: RiskDecision,
        position: PositionState,
        snapshots: Mapping[str, FeatureSnapshot],
        *,
        quantity_contracts: int | None = None,
        allow_partial_structure: bool = False,
    ) -> OrderPlan | None:
        now = self._clock.now_utc()
        orders: list[PlannedOrder] = []
        for leg in position.legs:
            snapshot = snapshots.get(leg.contract.symbol)
            if snapshot is None:
                if allow_partial_structure:
                    continue
                return None
            qty = (
                quantity_contracts
                if quantity_contracts is not None
                else leg.quantity_contracts
            )
            if qty <= 0 or qty > leg.quantity_contracts:
                return None
            exit_side = Side.SELL if leg.side is Side.BUY else Side.BUY
            limit = (
                snapshot.market.bid if exit_side is Side.SELL else snapshot.market.ask
            )
            if limit is None:
                return None
            internal_order_id = self._ids.new_id("ORD")
            partial = qty != leg.quantity_contracts
            exit_leg_id = (
                f"{leg.leg_id}-review-partial-{qty}"
                if partial
                else f"{leg.leg_id}-exit"
            )
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
                            quantity_contracts=qty,
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
                        quantity_contracts=qty,
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
            orders=_liability_first(orders),
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
        *,
        quantity_contracts: int | None = None,
        remaining_stays_open: bool = False,
        allow_partial_structure: bool = False,
    ) -> tuple[OrderEvent, ...]:
        record = self._services.store.get_position_lifecycle(position.trade_id)
        live = self._services.trade_manager.get_position(position.trade_id)
        if (
            record is not None
            and record.exit_order_ids
            and live is not None
            and live.state in {TradeState.EXIT_PENDING, TradeState.CLOSING}
        ):
            return self._adopt_existing_exit_orders(
                record, remaining_stays_open=remaining_stays_open
            )
        plan = self._exit_plan(
            intent,
            decision,
            position,
            snapshots,
            quantity_contracts=quantity_contracts,
            allow_partial_structure=allow_partial_structure,
        )
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
        persist_ids: tuple[str, ...] = () if remaining_stays_open else order_ids
        self._write_lifecycle(position.trade_id, exit_order_ids=order_ids)
        for event in submit.events:
            if event.state is OrderState.UNKNOWN:
                self._freeze_unknown_exit(position.trade_id)
                return submit.events
            self._maybe_record_fill_charges(event, decision=decision)
            self._services.trade_manager.apply_exit_order_event(
                event,
                capital_reservation_id=decision.capital_reservation_id,
                remaining_stays_open=remaining_stays_open,
            )
        self._write_lifecycle(position.trade_id, exit_order_ids=persist_ids)
        closed = self._services.trade_manager.get_position(position.trade_id)
        if closed is not None and closed.state is TradeState.CLOSED:
            if (
                intent.mode_id is not None
                and decision.recalculated_max_loss is not None
            ):
                self._services.gateway.note_mode_close(
                    intent.mode_id,
                    decision.recalculated_max_loss,
                    trade_id=position.trade_id,
                )
            self._open_book.pop(position.trade_id, None)
            self._record_campaign_close(position.trade_id)
            self._advance_roll_switch_after_close(position.trade_id)
        return submit.events

    def _advance_roll_switch_after_close(self, trade_id: str) -> None:
        """Move an in-flight roll/switch to replacement-pending once close fills."""
        existing = self._services.store.get_position_lifecycle(trade_id)
        if existing is None or existing.roll_switch_transition is None:
            return
        transition = transition_after_close(
            existing.roll_switch_transition,
            as_of=self._clock.now_utc(),
        )
        self._write_lifecycle(
            trade_id,
            roll_switch_transition=transition,
            patch_last_review_execution=ReviewExecutionStatus.REPLACEMENT_PENDING_L2,
        )

    def _adopt_existing_exit_orders(
        self,
        record: PositionLifecycleRecord,
        *,
        remaining_stays_open: bool = False,
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
                    remaining_stays_open=remaining_stays_open,
                )
        persist_ids = () if remaining_stays_open else record.exit_order_ids
        self._write_lifecycle(record.trade_id, exit_order_ids=persist_ids)
        closed = self._services.trade_manager.get_position(record.trade_id)
        if closed is not None and closed.state is TradeState.CLOSED:
            if (
                record.intent.mode_id is not None
                and decision.recalculated_max_loss is not None
            ):
                self._services.gateway.note_mode_close(
                    record.intent.mode_id,
                    decision.recalculated_max_loss,
                    trade_id=record.trade_id,
                )
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
        extra_reviews: tuple[PositionReviewRecord, ...] = (),
        extra_carry: tuple[PositionCarryRecord, ...] = (),
        roll_switch_transition: RollSwitchTransition | None = None,
        patch_last_review_execution: ReviewExecutionStatus | None = None,
        completed_roll_switch: ReviewAction | None = None,
    ) -> None:
        existing = self._services.store.get_position_lifecycle(trade_id)
        position = self._services.trade_manager.get_position(trade_id)
        book = self._open_book.get(trade_id)
        if book is not None:
            intent, decision = book
        elif existing is not None:
            intent, decision = existing.intent, existing.risk_decision
        else:
            return
        if position is None:
            if existing is None:
                return
            position = existing.position
        ids = (
            exit_order_ids
            if exit_order_ids is not None
            else (existing.exit_order_ids if existing is not None else ())
        )
        prior_reviews = existing.reviews if existing is not None else ()
        prior_carry = existing.carry_records if existing is not None else ()
        reviews = (*prior_reviews, *extra_reviews)
        if patch_last_review_execution is not None and reviews:
            last = reviews[-1]
            reviews = (
                *reviews[:-1],
                last.model_copy(
                    update={
                        "execution_status": patch_last_review_execution,
                        "completed_roll_switch": completed_roll_switch,
                    }
                ),
            )
        transition = roll_switch_transition
        if transition is None and existing is not None:
            transition = existing.roll_switch_transition
        campaign_id = self._trade_campaign.get(trade_id)
        if campaign_id is None and existing is not None:
            campaign_id = existing.campaign_id
        mode_id = intent.mode_id or (existing.mode_id if existing is not None else None)
        if campaign_id is not None and position.campaign_id != campaign_id:
            position = position.model_copy(update={"campaign_id": campaign_id})
        if mode_id is not None and position.mode_id != mode_id:
            position = position.model_copy(update={"mode_id": mode_id})
        record = PositionLifecycleRecord(
            trade_id=trade_id,
            position=position,
            intent=intent,
            risk_decision=decision,
            holding_style=_holding_style(intent),
            exit_order_ids=ids,
            reviews=reviews,
            carry_records=(*prior_carry, *extra_carry),
            roll_switch_transition=transition,
            as_of=position.as_of,
            mode_id=mode_id,
            campaign_id=campaign_id,
            policy_version=self._risk_policy.config.policy_version,
        )
        if existing is not None and _lifecycle_unchanged(existing, record):
            return
        self._services.store.upsert_position_lifecycle(
            record,
            event_id=self._ids.new_id("PLC"),
        )

    def _restore_campaign_ledger(self) -> None:
        """Rebuild campaign loss state from durable store before entries resume."""
        self._campaign_ledger = CampaignLedger.reconstruct_from_store(
            self._services.store
        )
        self._services.gateway.set_campaign_ledger(self._campaign_ledger)
        self._trade_campaign = {}
        self._campaign_close_recorded = set()
        for record in self._campaign_ledger.list_records():
            for trade_id in record.recorded_closes:
                self._campaign_close_recorded.add(trade_id)
        for persisted in self._services.store.list_position_lifecycle():
            if persisted.campaign_id is not None:
                self._trade_campaign[persisted.trade_id] = persisted.campaign_id
        for campaign_id in self._campaign_ledger.campaigns_blocking_entries():
            self._ensure_entries_blocked(
                ReasonCode.CAMPAIGN_LOSS_LIMIT,
                f"campaign {campaign_id} loss limit breached; replacements blocked",
            )

    def _ensure_campaign_on_entry(
        self,
        trade_id: str,
        *,
        intent: TradeIntent,
        request: PaperStrategyRequest,
    ) -> None:
        """Assign or inherit a durable campaign id for one opened trade."""
        mode_id = intent.mode_id
        if mode_id is None:
            return
        campaign_id = request.campaign_id or self._trade_campaign.get(trade_id)
        mode_book = self._services.gateway.mode_book
        if campaign_id is None:
            if mode_book is None:
                return
            campaign_id = self._ids.new_id("CAMP")
            loss_limit = campaign_loss_limit(mode_id, mode_book=mode_book)
            self._campaign_ledger.begin_campaign(
                campaign_id=campaign_id,
                mode_id=mode_id,
                loss_limit=loss_limit,
                trade_id=trade_id,
            )
            self._persist_campaign_record(campaign_id)
        elif self._campaign_ledger.get(campaign_id) is None:
            if mode_book is None:
                return
            loss_limit = campaign_loss_limit(mode_id, mode_book=mode_book)
            self._campaign_ledger.begin_campaign(
                campaign_id=campaign_id,
                mode_id=mode_id,
                loss_limit=loss_limit,
                trade_id=trade_id,
            )
            self._persist_campaign_record(campaign_id)
        else:
            self._link_campaign_trade(campaign_id, trade_id)
        self._trade_campaign[trade_id] = campaign_id

    def _link_campaign_trade(self, campaign_id: str, trade_id: str) -> None:
        """Attach a replacement trade to an existing campaign without resetting P&L."""
        if self._campaign_ledger.get(campaign_id) is None:
            return
        self._campaign_ledger.link_trade(campaign_id=campaign_id, trade_id=trade_id)
        self._trade_campaign[trade_id] = campaign_id
        self._persist_campaign_record(campaign_id)

    def _record_campaign_close(self, trade_id: str) -> None:
        """Accumulate realized gross, charges, and net into the durable campaign."""
        if trade_id in self._campaign_close_recorded:
            return
        lifecycle = self._services.store.get_position_lifecycle(trade_id)
        if lifecycle is None:
            return
        campaign_id = self._trade_campaign.get(trade_id) or lifecycle.campaign_id
        if campaign_id is None:
            return
        orders = index_order_events(self._services.store)
        charges_by_key = index_fill_charges(self._services.store)
        accounting = trade_accounting(
            lifecycle,
            orders,
            charges_by_key,
            charges_per_lot=self._risk_policy.config.charges_per_lot.to_money(),
        )
        self._campaign_ledger.record_close(
            campaign_id=campaign_id,
            trade_id=trade_id,
            accounting=accounting,
        )
        self._campaign_close_recorded.add(trade_id)
        self._persist_campaign_record(campaign_id)
        record = self._campaign_ledger.get(campaign_id)
        if record is not None and record.entries_blocked:
            self._ensure_entries_blocked(
                ReasonCode.CAMPAIGN_LOSS_LIMIT,
                f"campaign {campaign_id} loss limit breached after close",
            )

    def _persist_campaign_record(self, campaign_id: str) -> None:
        record = self._campaign_ledger.get(campaign_id)
        if record is None:
            return
        self._services.store.upsert_campaign_record(
            record,
            event_id=self._ids.new_id("CAM"),
        )

    def _freeze_unknown_exit(self, trade_id: str) -> None:
        self._ensure_entries_blocked(
            ReasonCode.UNKNOWN_ORDER_STATUS,
            "exit order status is unknown; replacement is blocked until reconciliation",
        )
        self._audit_lifecycle_alerts(
            (
                LifecycleAlert(
                    trade_id=trade_id,
                    reason_code=ReasonCode.UNKNOWN_ORDER_STATUS,
                    detail=(
                        "exit order status is unknown; replacement is blocked "
                        "until reconciliation"
                    ),
                ),
            )
        )

    def _audit_lifecycle_alerts(self, alerts: tuple[LifecycleAlert, ...]) -> None:
        now = self._clock.now_utc()
        existing = {
            (event.scope, event.reason_code)
            for stored in self._services.store.read_events()
            if stored.event_type is TradingEventType.RECONCILIATION_EVENT
            for event in (_as_reconciliation(stored.deserialize()),)
            if event is not None and not event.is_resolved
        }
        for alert in alerts:
            scope = f"trade/{alert.trade_id}/lifecycle"
            if (scope, alert.reason_code) in existing:
                continue
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

    def _restore_persisted_freeze(self) -> None:
        persisted = self._services.store.get_entry_freeze()
        if persisted is None or not persisted.entries_blocked:
            return
        if not self._services.controls.blocks_entry():
            self._services.controls.freeze_entries(
                actor="paper-lifecycle",
                scope="paper/entry-freeze",
                trigger=Trigger.RECONCILIATION,
                incident_id=self._ids.new_id("INC"),
            )

    def _entries_are_blocked(self, reconcile_blocked: bool) -> bool:
        persisted = self._services.store.get_entry_freeze()
        persisted_blocked = persisted is not None and persisted.entries_blocked
        derived = self._derived_block_reason() is not None
        return (
            reconcile_blocked
            or persisted_blocked
            or derived
            or self._services.controls.blocks_entry()
        )

    def _derived_block_reason(self) -> tuple[ReasonCode, str] | None:
        for position in self._services.trade_manager.list_positions():
            if position.protection_degraded or position.software_stop_unavailable:
                return (
                    ReasonCode.PROTECTION_DEGRADED,
                    position.unprotected_reason
                    or (
                        "required quotes are stale; software stop cannot evaluate "
                        "and PAPER has no broker-resident stop"
                    ),
                )
            if position.unprotected_reason:
                return (ReasonCode.UNPROTECTED_POSITION, position.unprotected_reason)
            if position.state is TradeState.EXIT_PENDING:
                return (
                    ReasonCode.UNKNOWN_ORDER_STATUS,
                    "trade is EXIT_PENDING; new entries stay blocked until "
                    "the in-flight exit reconciles",
                )
            if position.state is TradeState.REPAIR_REQUIRED:
                return (
                    ReasonCode.UNRECONCILED_POSITION,
                    "partial or incomplete multi-leg fill requires repair",
                )
            if (
                position.state.requires_protective_coverage
                and not position.protective_order_ids
            ):
                return (
                    ReasonCode.UNPROTECTED_POSITION,
                    "open position has no local protective stub coverage; "
                    "PAPER stops are software-only",
                )
        lifecycle_ids: set[str] = set()
        for persisted in self._services.store.list_position_lifecycle():
            if persisted.position.state is TradeState.CLOSED:
                continue
            lifecycle_ids.add(persisted.trade_id)
            if persisted.position.protection_degraded:
                return (
                    ReasonCode.PROTECTION_DEGRADED,
                    persisted.position.unprotected_reason
                    or (
                        "required quotes are stale; software stop cannot evaluate "
                        "and PAPER has no broker-resident stop"
                    ),
                )
            if persisted.position.state is TradeState.EXIT_PENDING:
                return (
                    ReasonCode.UNKNOWN_ORDER_STATUS,
                    "trade is EXIT_PENDING; new entries stay blocked until "
                    "the in-flight exit reconciles",
                )
            for order_id in persisted.exit_order_ids:
                event = self._exit_order_event(order_id)
                if event is None or event.state is OrderState.UNKNOWN:
                    return (
                        ReasonCode.UNKNOWN_ORDER_STATUS,
                        "exit order status is unknown; replacement is blocked "
                        "until reconciliation",
                    )
        broker_ids = {item.trade_id for item in self._services.broker.get_positions()}
        extra = broker_ids - lifecycle_ids
        if extra:
            return (
                ReasonCode.UNRECONCILED_POSITION,
                "broker position has no persisted exit lifecycle",
            )
        return None

    def _ensure_entries_blocked(self, reason: ReasonCode, detail: str) -> None:
        if not self._services.controls.blocks_entry():
            self._services.controls.freeze_entries(
                actor="paper-lifecycle",
                scope="paper/entry-freeze",
                trigger=Trigger.RECONCILIATION,
                incident_id=self._ids.new_id("INC"),
            )
        record = EntryFreezeRecord(
            entries_blocked=True,
            reason_code=reason,
            detail=detail,
            updated_at=self._clock.now_utc(),
        )
        self._services.store.upsert_entry_freeze(
            record,
            event_id=self._ids.new_id("FRZ"),
        )
        self._last_recovery = PositionRecoveryResult(
            restored_trade_ids=self._last_recovery.restored_trade_ids,
            entries_blocked=True,
            alerts=self._last_recovery.alerts,
            unprotected_trade_ids=self._last_recovery.unprotected_trade_ids,
            unreconciled_trade_ids=self._last_recovery.unreconciled_trade_ids,
        )

    def _maybe_release_entry_freeze(self, *, reconcile_blocked: bool) -> None:
        if reconcile_blocked:
            return
        controls = self._services.controls
        if controls.state.global_halt or controls.state.daily_loss_kill_switch:
            return
        derived = self._derived_block_reason()
        if derived is not None:
            self._ensure_entries_blocked(derived[0], derived[1])
            return
        persisted = self._services.store.get_entry_freeze()
        if persisted is not None and persisted.entries_blocked:
            released = EntryFreezeRecord(
                entries_blocked=False,
                reason_code=None,
                detail=None,
                updated_at=self._clock.now_utc(),
            )
            self._services.store.upsert_entry_freeze(
                released,
                event_id=self._ids.new_id("FRZ"),
            )
        if controls.state.entry_frozen:
            controls.release_entry_freeze(
                actor="paper-lifecycle",
                scope="paper/entry-freeze",
                trigger=Trigger.RECONCILIATION,
                incident_id=self._ids.new_id("INC"),
            )
        if controls.state.protection_degraded:
            controls.restore_protection(
                actor="paper-lifecycle",
                scope="paper/entry-freeze",
                trigger=Trigger.RECONCILIATION,
                incident_id=self._ids.new_id("INC"),
            )
        self._last_recovery = PositionRecoveryResult(
            restored_trade_ids=self._last_recovery.restored_trade_ids,
            entries_blocked=False,
            alerts=(),
            unprotected_trade_ids=self._last_recovery.unprotected_trade_ids,
            unreconciled_trade_ids=self._last_recovery.unreconciled_trade_ids,
        )

    def _diagnose_protection(
        self,
        position: PositionState,
        intent: TradeIntent,
        snapshots: Mapping[str, FeatureSnapshot],
    ) -> _ProtectionDiagnosis:
        watched = monitor_leg(intent)
        monitor_snap = snapshots.get(watched.contract.symbol)
        if monitor_snap is None:
            return _ProtectionDiagnosis(
                kind=_ProtectionKind.MISSING_MONITOR,
                reason_code=ReasonCode.UNPROTECTED_POSITION,
                detail=(
                    "monitor leg quote missing; software stop cannot evaluate "
                    "and PAPER has no broker-resident stop"
                ),
            )
        if self._quote_is_stale(monitor_snap):
            return _ProtectionDiagnosis(
                kind=_ProtectionKind.STALE,
                reason_code=ReasonCode.PROTECTION_DEGRADED,
                detail=(
                    "required quotes are stale; software stop cannot evaluate "
                    "and PAPER has no broker-resident stop"
                ),
            )
        if position.exit_policy.scope is ExitScope.STRATEGY_PNL:
            for leg in position.legs:
                snap = snapshots.get(leg.contract.symbol)
                if snap is None:
                    return _ProtectionDiagnosis(
                        kind=_ProtectionKind.MISSING_MONITOR,
                        reason_code=ReasonCode.UNPROTECTED_POSITION,
                        detail=(
                            "structure leg quote missing; software stop cannot "
                            "evaluate and PAPER has no broker-resident stop"
                        ),
                    )
                if self._quote_is_stale(snap):
                    return _ProtectionDiagnosis(
                        kind=_ProtectionKind.STALE,
                        reason_code=ReasonCode.PROTECTION_DEGRADED,
                        detail=(
                            "required quotes are stale; software stop cannot "
                            "evaluate and PAPER has no broker-resident stop"
                        ),
                    )
        return _ProtectionDiagnosis(
            kind=_ProtectionKind.OK,
            reason_code=ReasonCode.OK,
            detail="protection quotes are fresh",
        )

    def _note_exit_depth(
        self,
        feature: FeatureSnapshot,
        leg_snapshots: Mapping[str, FeatureSnapshot],
        gaps: list[str],
    ) -> None:
        """Record missing exit depth without blocking deterministic protection."""
        if self._paper_data is None:
            return
        seen: set[str] = set()
        for snap in (feature, *leg_snapshots.values()):
            if snap.snapshot_id in seen:
                continue
            seen.add(snap.snapshot_id)
            present, reason = observe_exit_depth(snap, self._paper_data)
            if not present:
                gaps.append(f"{snap.contract.symbol}:{reason}")

    def _quote_is_stale(self, snapshot: FeatureSnapshot) -> bool:
        if snapshot.quality.state.blocks_new_exposure:
            return True
        max_age_ms = self._account.config.freshness.quote_max_age_ms
        if max_age_ms is None:
            return True
        age = snapshot.times.age_at(self._clock.now_utc())
        return int(age.total_seconds() * 1000) > max_age_ms

    def _handle_missing_monitor(
        self,
        position: PositionState,
        intent: TradeIntent,
        decision: RiskDecision,
        snapshots: Mapping[str, FeatureSnapshot],
        diagnosis: _ProtectionDiagnosis,
    ) -> tuple[OrderEvent, ...]:
        self._mark_protection(
            position,
            degraded=False,
            unavailable=True,
            reason=diagnosis.detail,
        )
        self._ensure_entries_blocked(diagnosis.reason_code, diagnosis.detail)
        self._audit_lifecycle_alerts(
            (
                LifecycleAlert(
                    trade_id=position.trade_id,
                    reason_code=diagnosis.reason_code,
                    detail=diagnosis.detail,
                ),
            )
        )
        resolution = self._risk_policy.config.missing_monitor_resolution
        if resolution is not MissingMonitorResolution.FLATTEN_REMAINING:
            return ()
        quoted = {
            symbol: snap
            for symbol, snap in snapshots.items()
            if any(leg.contract.symbol == symbol for leg in position.legs)
        }
        if not quoted:
            return ()
        pending = self._services.trade_manager.apply_exit_evaluation(
            position.trade_id,
            ExitEvaluation(
                kind=ExitKind.STOP,
                reason_code=ReasonCode.UNPROTECTED_POSITION,
                detail=diagnosis.detail,
            ),
        )
        return self._submit_exit(
            intent,
            decision,
            pending,
            quoted,
            allow_partial_structure=True,
        )

    def _handle_stale_protection(
        self,
        position: PositionState,
        diagnosis: _ProtectionDiagnosis,
    ) -> None:
        now = self._clock.now_utc()
        since = position.protection_degraded_since or now
        self._mark_protection(
            position,
            degraded=True,
            unavailable=True,
            reason=diagnosis.detail,
            since=since,
        )
        self._ensure_entries_blocked(diagnosis.reason_code, diagnosis.detail)
        self._audit_lifecycle_alerts(
            (
                LifecycleAlert(
                    trade_id=position.trade_id,
                    reason_code=diagnosis.reason_code,
                    detail=diagnosis.detail,
                ),
            )
        )
        escalate_after = (
            self._account.config.freshness.protection_stale_escalate_after_ms
        )
        if escalate_after is None:
            return
        age_ms = int((now - since).total_seconds() * 1000)
        if age_ms < escalate_after:
            return
        self._audit_lifecycle_alerts(
            (
                LifecycleAlert(
                    trade_id=position.trade_id,
                    reason_code=ReasonCode.PROTECTION_DEGRADED,
                    detail=(
                        "stale protection did not recover within "
                        f"{escalate_after}ms; software stop remains unavailable "
                        "and is not broker-resident"
                    ),
                ),
            )
        )

    def _clear_protection_degraded(self, position: PositionState) -> None:
        if (
            not position.protection_degraded
            and not position.software_stop_unavailable
            and position.unprotected_reason is None
        ):
            return
        cleared = position.model_copy(
            update={
                "protection_degraded": False,
                "software_stop_unavailable": False,
                "protection_degraded_since": None,
                "unprotected_reason": None,
                "as_of": self._clock.now_utc(),
            }
        )
        self._services.trade_manager.restore_position(cleared)
        self._write_lifecycle(position.trade_id)
        # Stale path sets controls.protection_degraded; clear it or
        # blocks_entry() stays true after quotes recover.
        self._services.controls.restore_protection(
            actor="paper-protection",
            scope=f"trade/{position.trade_id}",
        )

    def _mark_protection(
        self,
        position: PositionState,
        *,
        degraded: bool,
        unavailable: bool,
        reason: str,
        since: datetime | None = None,
    ) -> None:
        now = self._clock.now_utc()
        updated = position.model_copy(
            update={
                "protection_degraded": degraded,
                "software_stop_unavailable": unavailable,
                "protection_degraded_since": (
                    since or position.protection_degraded_since or now
                ),
                "unprotected_reason": reason,
                "as_of": now,
            }
        )
        self._services.trade_manager.restore_position(updated)
        self._write_lifecycle(position.trade_id)
        if degraded:
            self._services.controls.degrade_protection(
                actor="paper-protection", scope=f"trade/{position.trade_id}"
            )
        else:
            self._services.controls.restore_protection(
                actor="paper-protection", scope=f"trade/{position.trade_id}"
            )
        self._services.store.upsert_protection_state(
            ProtectionStateRecord(
                trade_id=position.trade_id,
                status=(
                    ProtectionStatus.DEGRADED if degraded else ProtectionStatus.ACTIVE
                ),
                reason_code=(ReasonCode.PROTECTION_DEGRADED if degraded else None),
                degraded_since=updated.protection_degraded_since if degraded else None,
                last_heartbeat_at=now,
                monitor_symbols=tuple(leg.contract.symbol for leg in position.legs),
                as_of=now,
            )
        )

    def _evaluate_strategy(
        self,
        request: PaperStrategyRequest,
        *,
        system_state: SystemState,
        entries_blocked: bool,
        reconcile_id: str,
    ) -> _StrategyEvalRecord:
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
            early = PaperStrategyOutcome(
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
            return _StrategyEvalRecord(request=request, early_outcome=early)

        p0_block = self._paper_p0_block_reasons(request, skip_margin=True)
        if p0_block and request.execute:
            early = PaperStrategyOutcome(
                strategy_id=request.strategy_id,
                snapshot_id=request.underlying.snapshot_id,
                intents=(),
                rejection_reasons=p0_block,
                decisions=(),
                order_events=(),
                entry_blocked_reasons=p0_block,
                executed=request.execute,
                setup_features=request.setup_features,
                route_decision=request.route_decision,
                decision_quotes=_decision_quotes(request),
                execution_mode=request.execution_mode,
            )
            return _StrategyEvalRecord(request=request, early_outcome=early)

        try:
            strategy = build_strategy(request.strategy_id)
        except KeyError:
            early = PaperStrategyOutcome(
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
            return _StrategyEvalRecord(request=request, early_outcome=early)

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
            shortlist=request.shortlist,
            forced_mode_id=request.forced_mode_id,
            forced_family_id=request.forced_family_id,
            campaign_id=request.campaign_id,
        )

        if request.shortlist is not None:
            maybe_log_entry_shadow(
                request.shortlist,
                as_of=self._clock.now_utc(),
                decision_log=self._decision_log,
                enabled=True,
                run_id=f"RUN-ENTRY-{request.strategy_id}",
                environment=self._account.config.environment,
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
        intent_updates: dict[str, object] = {
            "setup_features": request.setup_features,
        }
        if request.forced_mode_id is not None:
            intent_updates["mode_id"] = request.forced_mode_id
        if request.forced_family_id is not None:
            intent_updates["family_id"] = request.forced_family_id
        intents = tuple(
            intent.model_copy(
                update={
                    **intent_updates,
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
            early = PaperStrategyOutcome(
                strategy_id=request.strategy_id,
                snapshot_id=request.underlying.snapshot_id,
                intents=(),
                rejection_reasons=rejection_reasons,
                decisions=(),
                order_events=(),
                strategy_version=getattr(strategy, "strategy_version", "unknown"),
                executed=request.execute,
                setup_features=request.setup_features,
                route_decision=request.route_decision,
                decision_quotes=_decision_quotes(request),
                execution_mode=request.execution_mode,
            )
            return _StrategyEvalRecord(request=request, early_outcome=early)

        if not request.execute:
            early = PaperStrategyOutcome(
                strategy_id=request.strategy_id,
                snapshot_id=request.underlying.snapshot_id,
                intents=intents,
                rejection_reasons=rejection_reasons,
                decisions=(),
                order_events=(),
                strategy_version=getattr(strategy, "strategy_version", "unknown"),
                executed=False,
                setup_features=request.setup_features,
                route_decision=request.route_decision,
                decision_quotes=_decision_quotes(request),
                execution_mode=request.execution_mode,
            )
            return _StrategyEvalRecord(request=request, early_outcome=early)

        return _StrategyEvalRecord(
            request=request,
            intents=intents,
            rejection_reasons=rejection_reasons,
            strategy=strategy,
            portfolio=portfolio,
            can_execute=True,
        )

    def _execute_strategy_eval(
        self,
        rec: _StrategyEvalRecord,
        *,
        arb_result: ArbitrationResult,
        entries_blocked: bool,
    ) -> PaperStrategyOutcome:
        if rec.early_outcome is not None:
            return rec.early_outcome

        request = rec.request
        strategy = rec.strategy
        portfolio = rec.portfolio
        intents = rec.intents

        approved_ids = {i.intent_id for i in arb_result.approved_intents}
        suppressions = {s.candidate_intent_id: s for s in arb_result.suppressed_intents}

        rejection_reasons = list(rec.rejection_reasons)
        risk_decisions: list[RiskDecision] = []
        order_events: list[OrderEvent] = []

        for intent in intents:
            if id(intent) in arb_result.suppressed_object_ids:
                rejection_reasons.append(ReasonCode.EXACT_DUPLICATE_SUPPRESSED)
                continue
            if arb_result.approved_object_ids:
                if id(intent) not in arb_result.approved_object_ids:
                    if intent.intent_id in suppressions:
                        rejection_reasons.append(ReasonCode.EXACT_DUPLICATE_SUPPRESSED)
                    continue
            else:
                if intent.intent_id in suppressions:
                    rejection_reasons.append(ReasonCode.EXACT_DUPLICATE_SUPPRESSED)
                    continue
                if intent.intent_id not in approved_ids:
                    continue

            instrument = _instrument_for(intent, request.instruments)
            if instrument is None:
                continue
            self._publish_quotes(intent, request)
            risk = self._services.gateway.evaluate(
                RiskGatewayRequest(
                    intent=intent,
                    feature_snapshot=_feature_for(intent, request),
                    portfolio_snapshot=portfolio,  # type: ignore[arg-type]
                    instrument=instrument,
                    leg_snapshots=_leg_snapshots(intent, request),
                    event_risk_state=request.event_risk_state,
                    paper_requirements=self._paper_data,
                    broker_state_ok=not entries_blocked,
                    campaign_id=request.campaign_id,
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
            rejection_reasons=tuple(rejection_reasons),
            decisions=tuple(risk_decisions),
            order_events=tuple(order_events),
            strategy_version=getattr(strategy, "strategy_version", "unknown"),
            executed=True,
            setup_features=request.setup_features,
            route_decision=request.route_decision,
            decision_quotes=_decision_quotes(request),
            execution_mode=request.execution_mode,
        )

    def _run_strategy(
        self,
        request: PaperStrategyRequest,
        *,
        system_state: SystemState,
        entries_blocked: bool,
        reconcile_id: str,
    ) -> PaperStrategyOutcome:
        rec = self._evaluate_strategy(
            request,
            system_state=system_state,
            entries_blocked=entries_blocked,
            reconcile_id=reconcile_id,
        )
        if not rec.can_execute:
            return rec.early_outcome  # type: ignore[return-value]
        arb = self._arbiter.arbitrate(
            rec.intents,
            existing_positions=self._services.trade_manager.list_positions(),
            now=self._clock.now_utc(),
        )
        return self._execute_strategy_eval(
            rec,
            arb_result=arb,
            entries_blocked=entries_blocked,
        )

    def _paper_p0_block_reasons(
        self,
        request: PaperStrategyRequest,
        *,
        skip_margin: bool,
    ) -> tuple[ReasonCode, ...]:
        """P0 paper-data gate. Margin is re-checked at Layer 2 after preview."""
        if self._paper_data is None:
            return ()
        snapshots = request.candidates if request.candidates else (request.underlying,)
        skip = (
            frozenset(
                {
                    PaperDataField.MARGIN_ESTIMATE,
                    PaperDataField.POSITION_BROKER_STATE,
                }
            )
            if skip_margin
            else frozenset()
        )
        assessment = assess_paper_data(
            self._paper_data,
            PaperDataInputs(
                now=self._clock.now_utc(),
                snapshots=snapshots,
                event_risk=request.event_risk_state,
                portfolio=None,
                broker_state_ok=True,
                margin_confirmed=None,
                margin_required=None,
                instruments=request.instruments,
                skip=skip,
            ),
        )
        if assessment.p0_ok:
            return ()
        return assessment.p0_reason_codes or (ReasonCode.DATA_GAP,)

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
        terminal_fail = False
        for event in submit.events:
            if event.state in {
                OrderState.REJECTED,
                OrderState.CANCELLED,
                OrderState.EXPIRED,
                OrderState.UNKNOWN,
            }:
                terminal_fail = True
                continue
            if event.state not in {OrderState.FILLED, OrderState.PARTIAL}:
                continue
            self._maybe_record_fill_charges(event, decision=risk)
            position = self._services.trade_manager.apply_order_event(
                event,
                intent=intent,
                capital_reservation_id=risk.capital_reservation_id,
            )
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

        final_position = self._services.trade_manager.get_position(trade_id)
        entry_complete = (
            final_position is not None
            and final_position.state is TradeState.OPEN
            and not self._services.trade_manager.is_pending(trade_id)
        )
        if entry_complete:
            self._ensure_campaign_on_entry(
                trade_id,
                intent=intent,
                request=request,
            )
            if intent.mode_id is not None and risk.recalculated_max_loss is not None:
                self._services.gateway.note_mode_fill(
                    intent.mode_id,
                    margin=risk.recalculated_max_loss,
                    trade_id=trade_id,
                )
            self._write_lifecycle(trade_id)
        elif terminal_fail:
            self._abort_incomplete_entry(intent, risk, trade_id=trade_id)
        return submit.events

    def _abort_incomplete_entry(
        self,
        intent: TradeIntent,
        risk: RiskDecision,
        *,
        trade_id: str,
    ) -> None:
        """Release holds after reject/abort/failed protected prefix; never submit."""
        if risk.capital_reservation_id is not None:
            try:
                self._services.reservations.release(
                    risk.capital_reservation_id,
                    trigger=Trigger.LOCAL_COMMAND,
                )
            except Exception:
                logger.exception(
                    "failed to release durable reservation on entry abort "
                    "trade_id=%s reservation=%s",
                    trade_id,
                    risk.capital_reservation_id,
                )
        if intent.mode_id is not None and risk.recalculated_max_loss is not None:
            self._services.gateway.note_mode_release(
                intent.mode_id,
                risk.recalculated_max_loss,
                trade_id=trade_id,
            )
        self._services.trade_manager.abort_pending_entry(trade_id)
        self._open_book.pop(trade_id, None)

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

    def _maybe_record_fill_charges(
        self,
        event: OrderEvent,
        *,
        decision: RiskDecision,
    ) -> None:
        lot_size = self._charge_policy.config.default_contracts_per_lot
        if decision.approved_legs:
            lot_size = decision.approved_legs[0].lot_size.contracts_per_lot
        record_fill_charge(
            self._services.store,
            event,
            policy=self._charge_policy.config,
            contracts_per_lot=lot_size,
            recorded_at=self._clock.now_utc(),
        )

    def _backfill_fill_charges(self) -> None:
        orders = index_order_events(self._services.store)
        contracts_by_trade: dict[str, int] = {}
        default_lot = self._charge_policy.config.default_contracts_per_lot
        for lifecycle in self._services.store.list_position_lifecycle():
            if lifecycle.risk_decision.approved_legs:
                contracts_by_trade[lifecycle.trade_id] = (
                    lifecycle.risk_decision.approved_legs[0].lot_size.contracts_per_lot
                )
        backfill_fill_charges(
            self._services.store,
            orders,
            policy=self._charge_policy.config,
            contracts_per_lot_by_trade=contracts_by_trade,
            default_contracts_per_lot=default_lot,
        )


def _stamp_roll_switch_replacement_request(
    request: PaperStrategyRequest,
    transition: RollSwitchTransition,
) -> PaperStrategyRequest:
    """Give a replacement leg fresh intent/order identity after structure close."""
    suffix = f"replace-{transition.transition_id}"
    underlying = request.underlying.model_copy(
        update={"snapshot_id": f"{request.underlying.snapshot_id}-{suffix}"}
    )
    candidates = tuple(
        candidate.model_copy(
            update={"snapshot_id": f"{candidate.snapshot_id}-{suffix}"}
        )
        for candidate in request.candidates
    )
    return PaperStrategyRequest(
        strategy_id=request.strategy_id,
        underlying=underlying,
        candidates=candidates,
        instruments=request.instruments,
        event_risk_state=request.event_risk_state,
        experiment_id=f"{request.experiment_id}::{transition.transition_id}",
        execution_mode=request.execution_mode,
        macro=request.macro,
        execute=request.execute,
        setup_features=request.setup_features,
        route_decision=request.route_decision,
        shortlist=request.shortlist,
        forced_mode_id=request.forced_mode_id,
        forced_family_id=request.forced_family_id,
        campaign_id=request.campaign_id,
    )


_index_order_events = index_order_events


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
    """Map each intent leg to its own candidate quote snapshot.

    Do not copy `intent.snapshot_id` onto option legs: that destroys quote
    provenance. Layer 2 validates freshness, skew and identity separately.
    """
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


def _eligible_for_scheduled_review(
    position: PositionState,
    intent: TradeIntent,
    persisted: PositionLifecycleRecord | None,
) -> bool:
    """M3/M4 and carried M2 receive 10:30/14:30 reviews; M1 is excluded."""
    mode_id = position.mode_id or intent.mode_id
    if mode_id is ModeId.M1_CAS:
        return False
    if mode_id in {
        ModeId.M3_TACTICAL_POSITIONAL,
        ModeId.M4_STRATEGIC_POSITIONAL,
    }:
        return True
    if mode_id is ModeId.M2_DIRECTIONAL:
        if persisted is None:
            return False
        return any(
            item.action is CarryGateAction.CARRY_APPROVED
            for item in persisted.carry_records
        )
    return True


def _review_already_recorded(
    prior: tuple[PositionReviewRecord, ...],
    *,
    slot_id: ReviewSlotId,
    session_date: date,
    missed_slot_ids: tuple[ReviewSlotId, ...],
) -> bool:
    if missed_slot_ids:
        missed = frozenset(missed_slot_ids)
        return any(
            item.session_date == session_date
            and (item.slot_id in missed or frozenset(item.missed_slot_ids) == missed)
            for item in prior
        )
    return any(
        item.slot_id is slot_id and item.session_date == session_date for item in prior
    )


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
        and existing.reviews == updated.reviews
        and existing.carry_records == updated.carry_records
        and existing.roll_switch_transition == updated.roll_switch_transition
        and existing.campaign_id == updated.campaign_id
        and existing.mode_id == updated.mode_id
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
                reason_code=ReasonCode.UNRECONCILED_POSITION,
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


def _matches_slot_venue(position: PositionState, venue: Exchange) -> bool:
    nse = {Exchange.NSE, Exchange.NFO}
    for leg in position.legs:
        if venue is Exchange.MCX:
            if leg.contract.exchange is not Exchange.MCX:
                return False
        elif leg.contract.exchange not in nse:
            return False
    return True


def _stamp_review(
    evaluation: ReviewEvaluation,
    *,
    trade_id: str,
    policy_id: str,
    slot_id: ReviewSlotId,
    session_date: date,
    review_id: str,
    submitted: bool,
    now: datetime,
    execution_status: ReviewExecutionStatus | None = None,
    missed_slot_ids: tuple[ReviewSlotId, ...] = (),
    next_slot_id: ReviewSlotId | None = None,
) -> PositionReviewRecord:
    tightened = (
        evaluation.updated_policy.stop_price
        if evaluation.action is ReviewAction.TIGHTEN_STOP
        and evaluation.updated_policy is not None
        else None
    )
    return PositionReviewRecord(
        review_id=review_id,
        trade_id=trade_id,
        slot_id=slot_id,
        session_date=session_date,
        action=evaluation.action,
        reason_code=evaluation.reason_code,
        detail=evaluation.detail,
        submitted=submitted
        and (
            not evaluation.action.is_proposal
            or execution_status is ReviewExecutionStatus.CLOSE_SUBMITTED
        ),
        frozen_policy_id=policy_id,
        tightened_stop_price=tightened,
        exit_quantity_contracts=evaluation.exit_quantity_contracts,
        execution_status=execution_status,
        missed_slot_ids=missed_slot_ids,
        next_slot_id=next_slot_id,
        as_of=now,
    )


def _hold_unavailable_review(
    *,
    trade_id: str,
    policy_id: str,
    slot_id: ReviewSlotId,
    session_date: date,
    review_id: str,
    now: datetime,
) -> PositionReviewRecord:
    return _unavailable_review(
        trade_id=trade_id,
        policy_id=policy_id,
        slot_id=slot_id,
        session_date=session_date,
        review_id=review_id,
        now=now,
        reason_code=ReasonCode.UNPROTECTED_POSITION,
        detail=(
            "monitor leg quote missing; software stop cannot evaluate "
            "and PAPER has no broker-resident stop"
        ),
    )


def _unavailable_review(
    *,
    trade_id: str,
    policy_id: str,
    slot_id: ReviewSlotId,
    session_date: date,
    review_id: str,
    now: datetime,
    reason_code: ReasonCode,
    detail: str,
) -> PositionReviewRecord:
    return PositionReviewRecord(
        review_id=review_id,
        trade_id=trade_id,
        slot_id=slot_id,
        session_date=session_date,
        action=ReviewAction.HOLD,
        reason_code=reason_code,
        detail=detail,
        submitted=False,
        frozen_policy_id=policy_id,
        as_of=now,
    )


def _stamp_carry(
    decision: CarryGateDecision,
    *,
    carry_id: str,
    now: datetime,
) -> PositionCarryRecord:
    return PositionCarryRecord(
        carry_id=carry_id,
        trade_id=decision.trade_id,
        session_date=decision.session_date,
        action=decision.action,
        mode_id=decision.mode_id,
        reason_code=decision.reason_code,
        detail=decision.detail,
        exit_initiated=decision.exit_initiated,
        as_of=now,
    )


def _remaining_dte(
    position: PositionState,
    intent: TradeIntent,
    snapshots: Mapping[str, FeatureSnapshot],
) -> int | None:
    watched = monitor_leg(intent)
    for leg in position.legs:
        if leg.leg_id != watched.leg_id:
            continue
        snapshot = snapshots.get(leg.contract.symbol)
        if snapshot is None or snapshot.derivatives is None:
            return None
        return snapshot.derivatives.days_to_expiry
    return None


def _as_reconciliation(payload: object) -> ReconciliationEvent | None:
    return payload if isinstance(payload, ReconciliationEvent) else None
