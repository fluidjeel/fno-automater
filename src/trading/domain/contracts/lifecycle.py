"""Persisted PAPER position lifecycle: frozen entry policy plus runtime state.

A filled position keeps the exit template and versions that existed at entry.
New config never rewrites an open trade. Protective stub IDs recorded here are
local software coverage, not broker-resident stop orders.
"""

from __future__ import annotations

from datetime import date

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.position import PositionState
from trading.domain.contracts.risk import RiskDecision
from trading.domain.enums import HoldingStyle, ReasonCode, ReviewAction, ReviewSlotId
from trading.domain.primitives import Price

__all__ = ["PositionLifecycleRecord", "PositionReviewRecord"]


class PositionReviewRecord(StrictModel):
    """One deterministic review decision persisted on the lifecycle snapshot."""

    review_id: NonEmptyStr
    trade_id: NonEmptyStr
    slot_id: ReviewSlotId
    session_date: date
    action: ReviewAction
    reason_code: ReasonCode
    detail: NonEmptyStr
    submitted: StrictBool
    frozen_policy_id: NonEmptyStr
    tightened_stop_price: Price | None = None
    exit_quantity_contracts: StrictInt | None = Field(default=None, gt=0)
    as_of: UtcDatetime

    @model_validator(mode="after")
    def _proposals_are_not_submitted(self) -> PositionReviewRecord:
        if self.action.is_proposal and self.submitted:
            raise ValueError(
                "HEDGE/ROLL is a new Layer 2 trade; auto-submit is blocked"
            )
        if self.action is ReviewAction.HOLD and self.submitted:
            raise ValueError("HOLD must not submit an order")
        if self.exit_quantity_contracts is not None and self.action not in {
            ReviewAction.PARTIAL_EXIT,
            ReviewAction.FULL_EXIT,
        }:
            raise ValueError(
                "exit quantity is only valid for PARTIAL_EXIT or FULL_EXIT"
            )
        return self


class PositionLifecycleRecord(VersionedModel):
    """Durable snapshot of one trade's exit lifecycle for restart recovery."""

    trade_id: NonEmptyStr
    position: PositionState
    intent: TradeIntent
    risk_decision: RiskDecision
    holding_style: HoldingStyle
    exit_order_ids: tuple[NonEmptyStr, ...] = ()
    reviews: tuple[PositionReviewRecord, ...] = ()
    as_of: UtcDatetime

    @model_validator(mode="after")
    def _identity_is_coherent(self) -> PositionLifecycleRecord:
        if self.position.trade_id != self.trade_id:
            raise ValueError("lifecycle trade_id must match position.trade_id")
        if self.position.intent_id != self.intent.intent_id:
            raise ValueError("lifecycle intent_id must match the frozen intent")
        if self.risk_decision.intent_id != self.intent.intent_id:
            raise ValueError("lifecycle risk decision must belong to the same intent")
        for review in self.reviews:
            if review.trade_id != self.trade_id:
                raise ValueError("review trade_id must match the lifecycle trade")
            if review.frozen_policy_id != self.position.exit_policy.policy_id:
                raise ValueError(
                    "review frozen_policy_id must match the position exit policy"
                )
        return self
