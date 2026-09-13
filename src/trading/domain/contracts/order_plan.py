"""OrderPlan: executable orders Layer 2 submits after risk approval."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.common import ContractRef
from trading.domain.contracts.order import OrderCommand, OrderIdentity
from trading.domain.enums import OrderPlanState, OrderType, Side
from trading.domain.primitives import Price

__all__ = ["OrderPlan", "PlannedOrder", "ProtectiveOrderStub"]


class PlannedOrder(StrictModel):
    """One entry order with stable identity for idempotent submission."""

    plan_leg_id: NonEmptyStr
    leg_id: NonEmptyStr
    identity: OrderIdentity
    command: OrderCommand
    plan_state: OrderPlanState = OrderPlanState.RISK_APPROVED

    @model_validator(mode="after")
    def _identity_matches_leg(self) -> PlannedOrder:
        if self.plan_state is OrderPlanState.CREATED:
            raise ValueError(
                "planned orders in an OrderPlan must be risk-approved before exposure"
            )
        return self


class ProtectiveOrderStub(StrictModel):
    """Protective coverage to place after entry. Invariant 16."""

    stub_id: NonEmptyStr
    contract: ContractRef
    side: Side
    order_type: OrderType
    quantity_contracts: StrictInt = Field(gt=0)
    trigger_price: Price | None = None
    limit_price: Price | None = None

    @model_validator(mode="after")
    def _stop_fields_match_type(self) -> ProtectiveOrderStub:
        needs_trigger = self.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}
        needs_limit = self.order_type in {
            OrderType.LIMIT,
            OrderType.STOP_LIMIT,
        }
        if needs_trigger and self.trigger_price is None:
            raise ValueError(
                f"{self.order_type} protective stub requires trigger_price"
            )
        if not needs_trigger and self.trigger_price is not None:
            raise ValueError(f"{self.order_type} must not carry trigger_price")
        if needs_limit and self.limit_price is None:
            raise ValueError(f"{self.order_type} protective stub requires limit_price")
        if not needs_limit and self.limit_price is not None:
            raise ValueError(f"{self.order_type} must not carry limit_price")
        return self


class OrderPlan(VersionedModel):
    """Approved executable order set for one intent."""

    plan_id: NonEmptyStr
    intent_id: NonEmptyStr
    risk_decision_id: NonEmptyStr
    correlation_id: NonEmptyStr
    policy_version: NonEmptyStr
    orders: tuple[PlannedOrder, ...]
    protective_orders: tuple[ProtectiveOrderStub, ...] = ()
    created_at: UtcDatetime
    expires_at: UtcDatetime

    @model_validator(mode="after")
    def _plan_is_coherent(self) -> OrderPlan:
        if not self.orders:
            raise ValueError("order plan must contain at least one entry order")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        leg_ids = [order.leg_id for order in self.orders]
        if len(set(leg_ids)) != len(leg_ids):
            raise ValueError("planned leg_id values must be unique")
        plan_leg_ids = [order.plan_leg_id for order in self.orders]
        if len(set(plan_leg_ids)) != len(plan_leg_ids):
            raise ValueError("plan_leg_id values must be unique")
        intent_ids = {order.identity.intent_id for order in self.orders}
        if intent_ids != {self.intent_id}:
            raise ValueError("every planned order must reference the plan intent_id")
        decision_ids = {order.identity.risk_decision_id for order in self.orders}
        if decision_ids != {self.risk_decision_id}:
            raise ValueError(
                "every planned order must reference the plan risk_decision_id"
            )
        return self

    def is_valid_at(self, now: datetime) -> bool:
        return self.created_at <= now < self.expires_at
