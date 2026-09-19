"""Capital reservation contract. Invariant 14."""

from __future__ import annotations

from pydantic import model_validator

from trading.domain.contracts.base import (
    NonEmptyStr,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import ReasonCode, ReservationState
from trading.domain.primitives import Money

__all__ = ["CapitalReservation"]


class CapitalReservation(VersionedModel):
    """Atomic capital hold tied to one intent."""

    reservation_id: NonEmptyStr
    intent_id: NonEmptyStr
    strategy_id: NonEmptyStr
    risk_decision_id: NonEmptyStr | None = None
    state: ReservationState
    amount: Money
    reason_codes: tuple[ReasonCode, ...] = ()
    created_at: UtcDatetime
    updated_at: UtcDatetime
    released_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _state_and_amount_agree(self) -> CapitalReservation:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        if self.released_at is not None and self.released_at < self.created_at:
            raise ValueError("released_at precedes created_at")

        if self.state is ReservationState.REJECTED:
            if not self.amount.is_zero:
                raise ValueError("rejected reservation must not hold capital")
            if not self.reason_codes or all(
                c is ReasonCode.OK for c in self.reason_codes
            ):
                raise ValueError("rejected reservation requires a non-OK reason code")
        elif self.amount.is_negative or self.amount.is_zero:
            raise ValueError("active reservation amount must be positive")

        if self.state is ReservationState.RELEASED and self.released_at is None:
            raise ValueError("released reservation must record released_at")
        if self.state.holds_capital and self.released_at is not None:
            raise ValueError("a holding reservation cannot already be released")
        return self
