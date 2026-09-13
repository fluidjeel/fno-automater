"""TradeIntent: desired exposure, never a broker command or a final quantity.

Invariant 3. The absence of certain fields is the design, and a test asserts it:

  - Legs carry a *ratio*, not a quantity. A 1x2 ratio spread is expressible; "buy
    150 contracts" is not. Layer 2 alone converts ratios into lots.
  - There is no order type, limit price, broker token or client order ID. Entry
    intent is expressed as a policy (offset in ticks, maximum spread, maximum
    slippage, timeout) that Layer 2 turns into an executable price at decision
    time using a current quote.
  - estimated_max_loss is mandatory and positive. Invariant 16 requires every
    position to be protected, and a structure whose worst case the strategy
    cannot state cannot be sized or protected.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.common import ContractRef
from trading.domain.enums import AssetClass, Side
from trading.domain.primitives import Money, Percent

__all__ = [
    "EntryPolicy",
    "ExitTemplate",
    "IntentConstraints",
    "IntentLeg",
    "TradeIntent",
]


class IntentLeg(StrictModel):
    """One leg of a structure, sized only relative to the other legs."""

    leg_id: NonEmptyStr
    contract: ContractRef
    side: Side
    ratio: StrictInt = Field(ge=1)

    @property
    def signed_ratio(self) -> int:
        return self.ratio if self.side is Side.BUY else -self.ratio


class EntryPolicy(StrictModel):
    """How the entry should be attempted, without naming an executable price."""

    limit_offset_ticks: StrictInt = 0
    max_spread: Percent
    max_slippage: Percent
    timeout_seconds: StrictInt = Field(gt=0)
    max_attempts: StrictInt = Field(default=1, ge=1)
    allow_market_fallback: StrictBool = False

    @model_validator(mode="after")
    def _tolerances_are_positive(self) -> EntryPolicy:
        if self.max_spread.fraction <= 0 or self.max_slippage.fraction <= 0:
            raise ValueError(
                "max_spread and max_slippage must be positive; an unbounded "
                "tolerance is not an execution constraint"
            )
        return self


class ExitTemplate(StrictModel):
    """Deterministic protection, complete before the position exists.

    Invariant 17: stops never widen after entry. trailing_activation_ticks and
    trailing_distance_ticks describe a monotonic tightening only; there is no
    field for widening a stop.
    """

    stop_distance_ticks: StrictInt = Field(gt=0)
    target_distance_ticks: StrictInt | None = Field(default=None, gt=0)
    break_even_trigger_ticks: StrictInt | None = Field(default=None, gt=0)
    trailing_activation_ticks: StrictInt | None = Field(default=None, gt=0)
    trailing_distance_ticks: StrictInt | None = Field(default=None, gt=0)
    time_exit: UtcDatetime | None = None
    exit_before_expiry_days: StrictInt | None = Field(default=None, ge=0)
    invalidation_note: NonEmptyStr
    partial_fill_policy: NonEmptyStr

    @model_validator(mode="after")
    def _trailing_is_fully_specified_and_tightening(self) -> ExitTemplate:
        activation = self.trailing_activation_ticks
        distance = self.trailing_distance_ticks
        if (activation is None) != (distance is None):
            raise ValueError(
                "a trailing stop needs both an activation and a distance; a "
                "half-specified trail cannot be applied deterministically"
            )
        if distance is not None and distance > self.stop_distance_ticks:
            raise ValueError(
                f"trailing distance {distance} exceeds the initial stop distance "
                f"{self.stop_distance_ticks}; a trail may only tighten (invariant 17)"
            )
        if (
            self.target_distance_ticks is not None
            and self.break_even_trigger_ticks is not None
            and self.break_even_trigger_ticks > self.target_distance_ticks
        ):
            raise ValueError(
                "break-even trigger sits beyond the target, so it could never fire"
            )
        return self


class IntentConstraints(StrictModel):
    """Conditions outside the structure itself that must hold to enter."""

    min_days_to_expiry: StrictInt = Field(ge=0)
    max_holding_days: StrictInt | None = Field(default=None, gt=0)
    require_event_blackout_clear: StrictBool = True
    min_open_interest: StrictInt | None = Field(default=None, ge=0)
    session_label: NonEmptyStr


class TradeIntent(VersionedModel):
    """An immutable strategy proposal for desired exposure."""

    intent_id: NonEmptyStr
    correlation_id: NonEmptyStr
    strategy_id: NonEmptyStr
    strategy_version: NonEmptyStr
    snapshot_id: NonEmptyStr
    promoted_config_version: NonEmptyStr
    promoted_proposal_id: NonEmptyStr | None = None
    supersedes_intent_id: NonEmptyStr | None = None
    underlying: NonEmptyStr
    asset_class: AssetClass
    legs: tuple[IntentLeg, ...]
    entry_policy: EntryPolicy
    exit_template: ExitTemplate
    constraints: IntentConstraints
    requested_risk: Money
    estimated_max_loss: Money
    setup_code: NonEmptyStr
    strategy_confidence: ExactDecimal = Field(ge=Decimal(0), le=Decimal(1))
    created_at: UtcDatetime
    expires_at: UtcDatetime

    @model_validator(mode="after")
    def _structure_is_coherent(self) -> TradeIntent:
        if not self.legs:
            raise ValueError("an intent must contain at least one leg")

        leg_ids = [leg.leg_id for leg in self.legs]
        if len(set(leg_ids)) != len(leg_ids):
            raise ValueError(
                "leg_id values must be unique; the idempotency key is derived from "
                "them, so a duplicate would collapse two orders into one"
            )

        wrong_underlying = {
            leg.contract.underlying
            for leg in self.legs
            if leg.contract.underlying != self.underlying
        }
        if wrong_underlying:
            raise ValueError(
                f"legs reference other underlyings {sorted(wrong_underlying)}; one "
                f"intent expresses exposure to {self.underlying} only"
            )

        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")

        if self.requested_risk.is_negative or self.requested_risk.is_zero:
            raise ValueError("requested_risk must be positive")
        if self.estimated_max_loss.is_negative or self.estimated_max_loss.is_zero:
            raise ValueError(
                "estimated_max_loss must be positive and defined; an undefined "
                "worst case cannot be sized or protected (invariant 16)"
            )
        if self.requested_risk.currency is not self.estimated_max_loss.currency:
            raise ValueError(
                "requested_risk and estimated_max_loss must share currency"
            )
        if self.estimated_max_loss < self.requested_risk:
            raise ValueError(
                f"estimated_max_loss {self.estimated_max_loss} is below requested_risk "
                f"{self.requested_risk}; the structure risks less than it asks for, "
                "which means one of the two is miscalculated"
            )

        if self.supersedes_intent_id == self.intent_id:
            raise ValueError("an intent cannot supersede itself")
        return self

    def is_live_at(self, now: datetime) -> bool:
        """Whether this intent may still be acted on at an injected instant."""
        return self.created_at <= now < self.expires_at
