"""Persisted PAPER position lifecycle: frozen entry policy plus runtime state.

A filled position keeps the exit template and versions that existed at entry.
New config never rewrites an open trade. Protective stub IDs recorded here are
local software coverage, not broker-resident stop orders.
"""

from __future__ import annotations

from pydantic import model_validator

from trading.domain.contracts.base import NonEmptyStr, UtcDatetime, VersionedModel
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.position import PositionState
from trading.domain.contracts.risk import RiskDecision
from trading.domain.enums import HoldingStyle

__all__ = ["PositionLifecycleRecord"]


class PositionLifecycleRecord(VersionedModel):
    """Durable snapshot of one trade's exit lifecycle for restart recovery."""

    trade_id: NonEmptyStr
    position: PositionState
    intent: TradeIntent
    risk_decision: RiskDecision
    holding_style: HoldingStyle
    exit_order_ids: tuple[NonEmptyStr, ...] = ()
    as_of: UtcDatetime

    @model_validator(mode="after")
    def _identity_is_coherent(self) -> PositionLifecycleRecord:
        if self.position.trade_id != self.trade_id:
            raise ValueError("lifecycle trade_id must match position.trade_id")
        if self.position.intent_id != self.intent.intent_id:
            raise ValueError("lifecycle intent_id must match the frozen intent")
        if self.risk_decision.intent_id != self.intent.intent_id:
            raise ValueError("lifecycle risk decision must belong to the same intent")
        return self
