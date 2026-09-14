"""Minimal valid contract instances for tests to perturb.

Every factory returns the smallest payload that passes validation, so a test can
change one field and attribute the failure to that field alone.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from trading.domain.contracts import (
    AIProposal,
    ApprovedLeg,
    CapitalReservation,
    ContractRef,
    DataQualityReport,
    EntryPolicy,
    EvidenceRef,
    ExitPolicy,
    ExitTemplate,
    ExposureSnapshot,
    FeatureSnapshot,
    IntentConstraints,
    IntentLeg,
    Lineage,
    MarketQuote,
    ModelVersions,
    OrderCommand,
    OrderEvent,
    OrderIdentity,
    OrderPlan,
    PendingOrderSummary,
    PlannedOrder,
    PortfolioSnapshot,
    PortfolioView,
    PositionLegState,
    PositionRecord,
    PositionState,
    ProtectiveOrderStub,
    ReconciliationEvent,
    ReconciliationResult,
    RiskDecision,
    SizingDecision,
    SizingLegResult,
    SizingLimits,
    SizingRequest,
    SnapshotTimes,
    TradeIntent,
    UnderlyingExposure,
    Versions,
)
from trading.domain.enums import (
    AssetClass,
    DataQuality,
    DifferenceClass,
    Exchange,
    ExitScope,
    InstrumentKind,
    OptionType,
    OrderPlanState,
    OrderState,
    OrderType,
    ProposalType,
    ReasonCode,
    Recommendation,
    ReconciliationTrigger,
    ReservationState,
    RiskAction,
    Severity,
    Side,
    SizingBindingConstraint,
    SystemState,
    TimeInForce,
    TradeState,
)
from trading.domain.primitives import (
    Currency,
    Lots,
    LotSize,
    Money,
    Percent,
    Price,
    TickSize,
)

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
INR = Currency.INR
TICK = TickSize.of("0.05")
LOT = LotSize(75)


def price(value: str) -> Price:
    return Price(Decimal(value), TICK)


def money(value: str) -> Money:
    return Money.of(value, INR)


def index_contract(**overrides: Any) -> ContractRef:
    return ContractRef.model_validate(
        {
            "exchange": Exchange.NSE,
            "symbol": "NIFTY",
            "instrument_kind": InstrumentKind.INDEX,
            "asset_class": AssetClass.EQUITY_INDEX,
            "underlying": "NIFTY",
            **overrides,
        }
    )


def future_contract(**overrides: Any) -> ContractRef:
    return ContractRef.model_validate(
        {
            "exchange": Exchange.MCX,
            "symbol": "CRUDEOIL26OCTFUT",
            "instrument_kind": InstrumentKind.FUTURE,
            "asset_class": AssetClass.COMMODITY,
            "underlying": "CRUDEOIL",
            "expiry": date(2026, 10, 17),
            **overrides,
        }
    )


def option_contract(**overrides: Any) -> ContractRef:
    return ContractRef.model_validate(
        {
            "exchange": Exchange.NFO,
            "symbol": "NIFTY26SEP24000CE",
            "instrument_kind": InstrumentKind.OPTION,
            "asset_class": AssetClass.EQUITY_INDEX,
            "underlying": "NIFTY",
            "expiry": date(2026, 9, 24),
            "strike": Decimal("24000"),
            "option_type": OptionType.CALL,
            **overrides,
        }
    )


def versions() -> Versions:
    return Versions(
        code_version="0.1.0", config_version="1", config_checksum="deadbeef"
    )


def lineage() -> Lineage:
    return Lineage(
        provider="fixture",
        source_ids=("src-1",),
        raw_event_refs=("offset-1",),
        normalization_version="1",
        versions=versions(),
    )


def quality(**overrides: Any) -> DataQualityReport:
    return DataQualityReport.model_validate(
        {
            "state": DataQuality.VALID,
            "age_ms": 120,
            "warmup_complete": True,
            "source_status": "connected",
            **overrides,
        }
    )


def snapshot_times(**overrides: Any) -> SnapshotTimes:
    return SnapshotTimes.model_validate(
        {
            "event_time": NOW,
            "source_time": NOW,
            "receive_time": NOW + timedelta(milliseconds=50),
            "calculation_time": NOW + timedelta(milliseconds=120),
            **overrides,
        }
    )


def quote(**overrides: Any) -> MarketQuote:
    return MarketQuote.model_validate(
        {
            "bid": price("100.00"),
            "ask": price("100.05"),
            "last": price("100.00"),
            **overrides,
        }
    )


def snapshot(**overrides: Any) -> FeatureSnapshot:
    return FeatureSnapshot.model_validate(
        {
            "snapshot_id": "SNAP-1",
            "contract": index_contract(),
            "times": snapshot_times(),
            "market": quote(),
            "feature_set_version": "1",
            "features": {"realized_vol_20d": Decimal("0.14")},
            "quality": quality(),
            "lineage": lineage(),
            **overrides,
        }
    )


def evidence(**overrides: Any) -> EvidenceRef:
    return EvidenceRef.model_validate(
        {
            "source_id": "rbi-policy",
            "published_at": NOW - timedelta(days=1),
            "retrieved_at": NOW - timedelta(hours=2),
            "allowlisted": True,
            "content_hash": "abc123",
            **overrides,
        }
    )


def proposal(**overrides: Any) -> AIProposal:
    return AIProposal.model_validate(
        {
            "proposal_id": "PROP-1",
            "proposal_type": ProposalType.MACRO_REGIME,
            "scope": "NIFTY/weekly",
            "as_of_time": NOW,
            "valid_until": NOW + timedelta(days=7),
            "versions": ModelVersions(
                model="m-1",
                prompt_version="p-1",
                retrieval_version="r-1",
                policy_version="pol-1",
            ),
            "recommendation": Recommendation.NEUTRAL,
            "confidence": Decimal("0.6"),
            "evidence": (evidence(),),
            **overrides,
        }
    )


def entry_policy(**overrides: Any) -> EntryPolicy:
    return EntryPolicy.model_validate(
        {
            "max_spread": Percent.from_percent("1"),
            "max_slippage": Percent.from_bps("25"),
            "timeout_seconds": 30,
            **overrides,
        }
    )


def exit_template(**overrides: Any) -> ExitTemplate:
    return ExitTemplate.model_validate(
        {
            "stop_distance_ticks": 200,
            "target_distance_ticks": 400,
            "invalidation_note": "underlying closes beyond short strike",
            "partial_fill_policy": "unwind filled legs at market within 60s",
            **overrides,
        }
    )


def constraints(**overrides: Any) -> IntentConstraints:
    return IntentConstraints.model_validate(
        {"min_days_to_expiry": 7, "session_label": "NSE_REGULAR", **overrides}
    )


def leg(leg_id: str = "leg-1", side: Side = Side.BUY, ratio: int = 1) -> IntentLeg:
    return IntentLeg(leg_id=leg_id, contract=option_contract(), side=side, ratio=ratio)


def intent(**overrides: Any) -> TradeIntent:
    return TradeIntent.model_validate(
        {
            "intent_id": "INT-1",
            "correlation_id": "COR-1",
            "strategy_id": "positional_index_options_poc",
            "strategy_version": "0.1.0",
            "snapshot_id": "SNAP-1",
            "promoted_config_version": "1",
            "underlying": "NIFTY",
            "asset_class": AssetClass.EQUITY_INDEX,
            "legs": (leg(),),
            "entry_policy": entry_policy(),
            "exit_template": exit_template(),
            "constraints": constraints(),
            "requested_risk": money("7000"),
            "estimated_max_loss": money("10000"),
            "setup_code": "IV_PCTL_HIGH",
            "strategy_confidence": Decimal("0.55"),
            "created_at": NOW,
            "expires_at": LATER,
            **overrides,
        }
    )


def exposure(**overrides: Any) -> ExposureSnapshot:
    return ExposureSnapshot.model_validate(
        {
            "as_of": NOW,
            "equity": money("700000"),
            "margin_used": money("0"),
            "margin_available": money("700000"),
            "open_trade_count": 0,
            "gross_notional": money("0"),
            "realized_pnl_today": money("0"),
            "unrealized_pnl": money("0"),
            **overrides,
        }
    )


def approved_leg(leg_id: str = "leg-1", lots: int = 1) -> ApprovedLeg:
    return ApprovedLeg(leg_id=leg_id, lots=Lots(lots), lot_size=LOT)


def risk_decision(**overrides: Any) -> RiskDecision:
    return RiskDecision.model_validate(
        {
            "decision_id": "DEC-1",
            "intent_id": "INT-1",
            "correlation_id": "COR-1",
            "policy_version": "1",
            "config_version": "1",
            "action": RiskAction.APPROVE,
            "approved_legs": (approved_leg(),),
            "capital_reservation_id": "RES-1",
            "reserved_capital": money("10000"),
            "recalculated_max_loss": money("9800"),
            "margin_required": money("12000"),
            "pre_trade_exposure": exposure(),
            "post_trade_projection": exposure(margin_used=money("12000")),
            "reason_codes": (ReasonCode.OK,),
            "decided_at": NOW,
            "expires_at": NOW + timedelta(minutes=5),
            **overrides,
        }
    )


def order_identity(**overrides: Any) -> OrderIdentity:
    return OrderIdentity.model_validate(
        {
            "internal_order_id": "ORD-1",
            "client_order_id": "CLI-1",
            "idempotency_key": "83e65c7aafd8d0272bdd320d1d437938",
            "broker_order_id": "BRK-1",
            "intent_id": "INT-1",
            "risk_decision_id": "DEC-1",
            "trade_id": "TRD-1",
            "correlation_id": "COR-1",
            **overrides,
        }
    )


def order_command(**overrides: Any) -> OrderCommand:
    return OrderCommand.model_validate(
        {
            "contract": option_contract(),
            "side": Side.BUY,
            "order_type": OrderType.LIMIT,
            "time_in_force": TimeInForce.DAY,
            "quantity_contracts": 75,
            "limit_price": price("120.00"),
            **overrides,
        }
    )


def order_event(**overrides: Any) -> OrderEvent:
    return OrderEvent.model_validate(
        {
            "event_id": "EVT-1",
            "identity": order_identity(),
            "command": order_command(),
            "state": OrderState.ACKNOWLEDGED,
            "attempt_number": 1,
            "acknowledged_quantity": 75,
            "sent_at": NOW,
            "received_at": NOW + timedelta(milliseconds=80),
            **overrides,
        }
    )


def reconciliation_event(**overrides: Any) -> ReconciliationEvent:
    return ReconciliationEvent.model_validate(
        {
            "event_id": "REC-1",
            "scope": "account/ACC-1/orders",
            "trigger": ReconciliationTrigger.BOOT,
            "difference_class": DifferenceClass.NONE,
            "severity": Severity.INFO,
            "reason_code": ReasonCode.OK,
            "entries_blocked": False,
            "detected_at": NOW,
            "resolved_at": NOW,
            **overrides,
        }
    )


def position_record(**overrides: Any) -> PositionRecord:
    return PositionRecord.model_validate(
        {
            "trade_id": "TRD-OPEN-1",
            "strategy_id": "positional_index_options_poc",
            "contract": option_contract(),
            "side": Side.BUY,
            "quantity_contracts": 75,
            "average_price": price("118.50"),
            "unrealized_pnl": money("250"),
            **overrides,
        }
    )


def pending_order(**overrides: Any) -> PendingOrderSummary:
    return PendingOrderSummary.model_validate(
        {
            "internal_order_id": "ORD-PEND-1",
            "intent_id": "INT-1",
            "contract": option_contract(),
            "side": Side.BUY,
            "quantity_contracts": 75,
            "state": OrderState.ACKNOWLEDGED,
            **overrides,
        }
    )


def underlying_exposure(**overrides: Any) -> UnderlyingExposure:
    return UnderlyingExposure.model_validate(
        {
            "underlying": "NIFTY",
            "net_delta": 75,
            "gross_notional": money("900000"),
            "open_position_count": 1,
            **overrides,
        }
    )


def portfolio_snapshot(**overrides: Any) -> PortfolioSnapshot:
    return PortfolioSnapshot.model_validate(
        {
            "portfolio_snapshot_id": "PORT-1",
            "account_id": "ACC-1",
            "as_of": NOW,
            "exposure": exposure(),
            "positions": (),
            "pending_orders": (),
            "reserved_capital": money("0"),
            "underlying_exposure": (),
            "versions": versions(),
            **overrides,
        }
    )


def portfolio_view(**overrides: Any) -> PortfolioView:
    return PortfolioView.model_validate(
        {
            "as_of": NOW,
            "open_trade_count": 0,
            "margin_available": money("700000"),
            "realized_pnl_today": money("0"),
            "net_delta": 0,
            "system_state": SystemState.READY,
            "entries_permitted": True,
            **overrides,
        }
    )


def sizing_limits(**overrides: Any) -> SizingLimits:
    return SizingLimits.model_validate(
        {
            "policy_version": "1",
            "config_version": "1",
            "max_loss_per_trade": money("7000"),
            "daily_loss_remaining": money("21000"),
            "strategy_allocation_remaining": money("140000"),
            "margin_available": money("700000"),
            **overrides,
        }
    )


def sizing_request(**overrides: Any) -> SizingRequest:
    snap = snapshot()
    return SizingRequest.model_validate(
        {
            "request_id": "SIZE-REQ-1",
            "intent": intent(snapshot_id=snap.snapshot_id),
            "feature_snapshot": snap,
            "portfolio_snapshot": portfolio_snapshot(),
            "limits": sizing_limits(),
            "requested_at": NOW,
            **overrides,
        }
    )


def sizing_leg_result(leg_id: str = "leg-1", lots: int = 1) -> SizingLegResult:
    return SizingLegResult(leg_id=leg_id, lots=Lots(lots), lot_size=LOT)


def sizing_decision(**overrides: Any) -> SizingDecision:
    return SizingDecision.model_validate(
        {
            "sizing_id": "SIZE-1",
            "request_id": "SIZE-REQ-1",
            "intent_id": "INT-1",
            "binding_constraint": SizingBindingConstraint.RISK,
            "approved_legs": (sizing_leg_result(),),
            "risk_lots": 1,
            "capital_lots": 2,
            "margin_lots": 3,
            "portfolio_limit_lots": 4,
            "liquidity_lots": 5,
            "estimated_margin": money("12000"),
            "recalculated_max_loss": money("9800"),
            "decided_at": NOW,
            **overrides,
        }
    )


def capital_reservation(**overrides: Any) -> CapitalReservation:
    return CapitalReservation.model_validate(
        {
            "reservation_id": "RES-1",
            "intent_id": "INT-1",
            "strategy_id": "positional_index_options_poc",
            "risk_decision_id": "DEC-1",
            "state": ReservationState.RESERVED,
            "amount": money("10000"),
            "created_at": NOW,
            "updated_at": NOW,
            **overrides,
        }
    )


def planned_order(**overrides: Any) -> PlannedOrder:
    return PlannedOrder.model_validate(
        {
            "plan_leg_id": "PLAN-LEG-1",
            "leg_id": "leg-1",
            "identity": order_identity(),
            "command": order_command(),
            "plan_state": OrderPlanState.RISK_APPROVED,
            **overrides,
        }
    )


def protective_stub(**overrides: Any) -> ProtectiveOrderStub:
    return ProtectiveOrderStub.model_validate(
        {
            "stub_id": "PROT-1",
            "contract": option_contract(),
            "side": Side.SELL,
            "order_type": OrderType.STOP,
            "quantity_contracts": 75,
            "trigger_price": price("95.00"),
            **overrides,
        }
    )


def order_plan(**overrides: Any) -> OrderPlan:
    return OrderPlan.model_validate(
        {
            "plan_id": "PLAN-1",
            "intent_id": "INT-1",
            "risk_decision_id": "DEC-1",
            "correlation_id": "COR-1",
            "policy_version": "1",
            "orders": (planned_order(),),
            "protective_orders": (protective_stub(),),
            "created_at": NOW,
            "expires_at": LATER,
            **overrides,
        }
    )


def exit_policy(**overrides: Any) -> ExitPolicy:
    return ExitPolicy.model_validate(
        {
            "policy_id": "EXIT-POL-1",
            "trade_id": "TRD-1",
            "scope": ExitScope.LEG_PRICE,
            "initial_stop_distance_ticks": 200,
            "current_stop_distance_ticks": 200,
            "stop_price": price("95.00"),
            "target_price": price("140.00"),
            "initialized_at": NOW,
            **overrides,
        }
    )


def position_leg_state(**overrides: Any) -> PositionLegState:
    return PositionLegState.model_validate(
        {
            "leg_id": "leg-1",
            "contract": option_contract(),
            "side": Side.BUY,
            "quantity_contracts": 75,
            "average_entry_price": price("118.50"),
            "current_stop_price": price("95.00"),
            **overrides,
        }
    )


def position_state(**overrides: Any) -> PositionState:
    return PositionState.model_validate(
        {
            "trade_id": "TRD-1",
            "intent_id": "INT-1",
            "strategy_id": "positional_index_options_poc",
            "state": TradeState.OPEN,
            "legs": (position_leg_state(),),
            "exit_policy": exit_policy(),
            "protective_order_ids": ("PROT-ORD-1",),
            "opened_at": NOW,
            "as_of": NOW,
            **overrides,
        }
    )


def reconciliation_result(**overrides: Any) -> ReconciliationResult:
    return ReconciliationResult.model_validate(
        {
            "result_id": "RECON-RUN-1",
            "trigger": ReconciliationTrigger.BOOT,
            "events": (reconciliation_event(),),
            "entries_blocked": False,
            "prior_system_state": SystemState.RECOVERY,
            "resulting_system_state": SystemState.READY,
            "started_at": NOW,
            "completed_at": NOW + timedelta(seconds=2),
            **overrides,
        }
    )


ALL_FACTORIES = (
    index_contract,
    option_contract,
    quality,
    snapshot_times,
    quote,
    snapshot,
    evidence,
    proposal,
    entry_policy,
    exit_template,
    constraints,
    intent,
    exposure,
    risk_decision,
    order_identity,
    order_command,
    order_event,
    reconciliation_event,
    portfolio_snapshot,
    portfolio_view,
    sizing_request,
    sizing_decision,
    capital_reservation,
    order_plan,
    position_state,
    reconciliation_result,
)
