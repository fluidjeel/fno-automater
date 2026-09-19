"""RiskDecision to OrderPlan conversion with executable prices."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from trading.domain.clock import Clock
from trading.domain.contracts.intent import IntentLeg, TradeIntent
from trading.domain.contracts.order import OrderCommand, OrderIdentity
from trading.domain.contracts.order_plan import (
    OrderPlan,
    PlannedOrder,
    ProtectiveOrderStub,
)
from trading.domain.contracts.risk import ApprovedLeg, RiskDecision
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import (
    OrderPlanState,
    OrderType,
    ReasonCode,
    Side,
    TimeInForce,
)
from trading.domain.ids import IdFactory, derive_idempotency_key
from trading.domain.primitives import Price, Rounding

__all__ = ["OrderPlanPlanner", "OrderPlanRequest", "PlannerError"]


class PlannerError(Exception):
    """Raised when an OrderPlan cannot be built deterministically."""

    def __init__(self, reason_code: ReasonCode, detail: str) -> None:
        self.reason_code = reason_code
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class OrderPlanRequest:
    """Inputs required to build one executable order plan."""

    intent: TradeIntent
    decision: RiskDecision
    feature_snapshot: FeatureSnapshot
    account_id: str
    leg_snapshots: Mapping[str, FeatureSnapshot] = field(default_factory=dict)


class OrderPlanPlanner:
    """Turn an approved RiskDecision into an executable OrderPlan."""

    def __init__(self, *, clock: Clock, id_factory: IdFactory) -> None:
        self._clock = clock
        self._ids = id_factory

    def build(self, request: OrderPlanRequest) -> OrderPlan:
        """Build an OrderPlan with stable identities and protective stubs."""
        now = self._clock.now_utc()
        intent = request.intent
        decision = request.decision
        feature = request.feature_snapshot

        if not decision.permits_submission:
            raise PlannerError(
                ReasonCode.RISK_LIMIT_TRADE,
                f"risk action {decision.action} does not permit submission",
            )
        if decision.intent_id != intent.intent_id:
            raise PlannerError(
                ReasonCode.SNAPSHOT_MISMATCH,
                "risk decision intent_id does not match the trade intent",
            )
        if not decision.is_valid_at(now):
            raise PlannerError(
                ReasonCode.DECISION_EXPIRED,
                "risk decision is expired or not yet valid",
            )
        if feature.snapshot_id != intent.snapshot_id:
            raise PlannerError(
                ReasonCode.SNAPSHOT_MISMATCH,
                "feature snapshot does not match the trade intent",
            )

        timeout_deadline = now + timedelta(seconds=intent.entry_policy.timeout_seconds)
        expires_at = min(decision.expires_at, intent.expires_at, timeout_deadline)
        if expires_at <= now:
            raise PlannerError(
                ReasonCode.DECISION_EXPIRED,
                "order plan would expire immediately",
            )

        legs_by_id = {leg.leg_id: leg for leg in intent.legs}
        planned_orders: list[PlannedOrder] = []
        protective_orders: list[ProtectiveOrderStub] = []
        trade_id = self._ids.new_id("TRD")

        for approved in decision.approved_legs:
            intent_leg = legs_by_id.get(approved.leg_id)
            if intent_leg is None:
                raise PlannerError(
                    ReasonCode.INSTRUMENT_UNKNOWN,
                    f"approved leg {approved.leg_id} is absent from the intent",
                )
            leg_feature = request.leg_snapshots.get(
                approved.leg_id,
                feature,
            )
            limit_price = _entry_limit_price(
                intent_leg.side,
                leg_feature,
                intent.entry_policy.limit_offset_ticks,
            )
            quantity_contracts = approved.quantity.contracts
            internal_order_id = self._ids.new_id("ORD")
            idempotency_key = derive_idempotency_key(
                account_id=request.account_id,
                strategy_id=intent.strategy_id,
                strategy_version=intent.strategy_version,
                intent_id=intent.intent_id,
                leg_id=approved.leg_id,
                side=intent_leg.side.value,
                quantity_contracts=quantity_contracts,
            )
            identity = OrderIdentity(
                internal_order_id=internal_order_id,
                client_order_id=internal_order_id,
                idempotency_key=idempotency_key,
                intent_id=intent.intent_id,
                risk_decision_id=decision.decision_id,
                trade_id=trade_id,
                correlation_id=intent.correlation_id,
                experiment_id=intent.experiment_id,
                execution_mode=intent.execution_mode,
            )
            command = OrderCommand(
                contract=intent_leg.contract,
                side=intent_leg.side,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                quantity_contracts=quantity_contracts,
                limit_price=limit_price,
            )
            planned_orders.append(
                PlannedOrder(
                    plan_leg_id=f"{approved.leg_id}-entry",
                    leg_id=approved.leg_id,
                    identity=identity,
                    command=command,
                    plan_state=OrderPlanState.RISK_APPROVED,
                )
            )
            protective_orders.append(
                _protective_stub(
                    stub_id=self._ids.new_id("PROT"),
                    intent_leg=intent_leg,
                    approved=approved,
                    entry_price=limit_price,
                    stop_distance_ticks=intent.exit_template.stop_distance_ticks,
                )
            )

        return OrderPlan(
            plan_id=self._ids.new_id("PLAN"),
            intent_id=intent.intent_id,
            risk_decision_id=decision.decision_id,
            correlation_id=intent.correlation_id,
            policy_version=decision.policy_version,
            orders=tuple(planned_orders),
            protective_orders=tuple(protective_orders),
            created_at=now,
            expires_at=expires_at,
        )


def _entry_limit_price(
    side: Side,
    feature: FeatureSnapshot,
    offset_ticks: int,
) -> Price:
    quote = feature.market
    reference = quote.ask if side is Side.BUY else quote.bid
    if reference is None:
        quote_side = "ask" if side is Side.BUY else "bid"
        raise PlannerError(
            ReasonCode.PRICE_UNAVAILABLE,
            f"{side.value} entry requires a {quote_side} quote",
        )
    tick = reference.tick
    adjusted = reference.value + Decimal(offset_ticks) * tick.value
    return Price.snap(adjusted, tick)


def _protective_stub(
    *,
    stub_id: str,
    intent_leg: IntentLeg,
    approved: ApprovedLeg,
    entry_price: Price,
    stop_distance_ticks: int,
) -> ProtectiveOrderStub:
    tick = entry_price.tick
    stop_value = entry_price.value - Decimal(stop_distance_ticks) * tick.value
    trigger_price = Price.snap(stop_value, tick, rounding=Rounding.FLOOR)
    exit_side = Side.SELL if intent_leg.side is Side.BUY else Side.BUY
    return ProtectiveOrderStub(
        stub_id=stub_id,
        contract=intent_leg.contract,
        side=exit_side,
        order_type=OrderType.STOP,
        quantity_contracts=approved.quantity.contracts,
        trigger_price=trigger_price,
    )
