"""Mode 2 overnight carry gate contracts (Phase P9 / spec §4.2)."""

from __future__ import annotations

from datetime import date

from pydantic import Field

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import CarryGateAction, ModeId, ReasonCode
from trading.domain.primitives import Money

__all__ = [
    "CarryGateDecision",
    "M2CarryGateConfig",
    "M2CarryGateInput",
    "PositionCarryRecord",
]


class M2CarryGateConfig(StrictModel):
    """Validated carry gate thresholds for Mode 2."""

    schema_version: NonEmptyStr = "1"
    min_remaining_dte: StrictInt = Field(ge=0)
    overnight_loss_budget_fraction: ExactDecimal


class M2CarryGateInput(StrictModel):
    """Evidence bundle evaluated at entry cutoff. P&L is observability only."""

    mode_id: ModeId
    thesis_evaluated_today: StrictBool
    thesis_still_valid: StrictBool
    remaining_dte: StrictInt = Field(ge=0)
    overnight_max_loss: Money
    overnight_loss_budget: Money
    event_blackout: StrictBool
    portfolio_entries_blocked: StrictBool
    mode_daily_loss_breached: StrictBool
    exit_policy_persisted: StrictBool
    recovery_healthy: StrictBool
    current_pnl: Money | None = None


class CarryGateDecision(VersionedModel):
    """Deterministic carry gate outcome for one trade and session."""

    decision_id: NonEmptyStr
    trade_id: NonEmptyStr
    session_date: date
    action: CarryGateAction
    mode_id: ModeId
    reason_code: ReasonCode
    detail: NonEmptyStr
    exit_initiated: StrictBool
    as_of: UtcDatetime


class PositionCarryRecord(StrictModel):
    """Persisted carry decision on a lifecycle snapshot."""

    carry_id: NonEmptyStr
    trade_id: NonEmptyStr
    session_date: date
    action: CarryGateAction
    mode_id: ModeId
    reason_code: ReasonCode
    detail: NonEmptyStr
    exit_initiated: StrictBool
    as_of: UtcDatetime
