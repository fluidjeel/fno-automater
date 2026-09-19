"""Runtime position and exit policy state."""

from __future__ import annotations

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.common import ContractRef
from trading.domain.enums import ExecutionMode, ExitScope, Side, TradeState
from trading.domain.primitives import Money, Price

__all__ = ["ExitPolicy", "PositionLegState", "PositionState"]


class ExitPolicy(VersionedModel):
    """Runtime exit state initialized from an ExitTemplate at entry."""

    policy_id: NonEmptyStr
    trade_id: NonEmptyStr
    scope: ExitScope
    initial_stop_distance_ticks: StrictInt = Field(gt=0)
    current_stop_distance_ticks: StrictInt = Field(gt=0)
    stop_price: Price | None = None
    target_price: Price | None = None
    strategy_entry_pnl: Money | None = None
    pnl_stop: Money | None = None
    pnl_target: Money | None = None
    trailing_active: StrictBool = False
    breakeven_active: StrictBool = False
    time_exit: UtcDatetime | None = None
    exit_before_expiry_days: StrictInt | None = Field(default=None, ge=0)
    initialized_at: UtcDatetime

    @model_validator(mode="after")
    def _stops_never_widen(self) -> ExitPolicy:
        if self.current_stop_distance_ticks > self.initial_stop_distance_ticks:
            raise ValueError(
                f"current stop distance {self.current_stop_distance_ticks} exceeds "
                f"initial {self.initial_stop_distance_ticks}; stops may only tighten "
                "(invariant 17)"
            )
        return self


class PositionLegState(StrictModel):
    """One leg of an open trade."""

    leg_id: NonEmptyStr
    contract: ContractRef
    side: Side
    quantity_contracts: StrictInt = Field(gt=0)
    average_entry_price: Price
    current_stop_price: Price | None = None


class PositionState(VersionedModel):
    """Operational mirror of one open or closing trade."""

    trade_id: NonEmptyStr
    intent_id: NonEmptyStr
    strategy_id: NonEmptyStr
    strategy_version: NonEmptyStr
    experiment_id: NonEmptyStr
    execution_mode: ExecutionMode
    state: TradeState
    legs: tuple[PositionLegState, ...]
    exit_policy: ExitPolicy
    protective_order_ids: tuple[NonEmptyStr, ...] = ()
    opened_at: UtcDatetime | None = None
    as_of: UtcDatetime
    protection_degraded: StrictBool = False
    software_stop_unavailable: StrictBool = False
    protection_degraded_since: UtcDatetime | None = None
    unprotected_reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _state_matches_coverage_requirement(self) -> PositionState:
        if not self.legs:
            raise ValueError("position must contain at least one leg")
        if self.exit_policy.trade_id != self.trade_id:
            raise ValueError("exit policy trade_id must match position trade_id")
        needs_protection = self.state in {
            TradeState.OPEN,
            TradeState.EXIT_PENDING,
            TradeState.CLOSING,
            TradeState.REPAIR_REQUIRED,
        }
        if needs_protection and not self.protective_order_ids:
            raise ValueError(
                f"state {self.state} requires protective coverage (invariant 16)"
            )
        if self.state is TradeState.OPEN and self.opened_at is None:
            raise ValueError("open position must record opened_at")
        return self
