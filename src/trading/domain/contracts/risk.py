"""RiskDecision: Layer 2's answer to a Trade Intent.

Invariant 4: Layer 2 independently validates every authoritative financial value,
so recalculated_max_loss lives here and is not copied from the intent.

Invariant 14: capital is reserved before submission. An approval therefore must
carry a reservation ID, and a rejection must not.

DOMAIN_CONTRACTS.md: "Expired approval must be recalculated", hence expires_at.
"""

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
from trading.domain.contracts.common import ExposureSnapshot
from trading.domain.enums import ReasonCode, RiskAction
from trading.domain.primitives import Lots, LotSize, Money, Quantity

__all__ = ["ApprovedLeg", "RiskDecision"]

_APPROVING_ACTIONS = frozenset({RiskAction.APPROVE, RiskAction.RESIZE})


class ApprovedLeg(StrictModel):
    """Final quantity for one leg. Only Layer 2 may produce this."""

    leg_id: NonEmptyStr
    lots: Lots
    lot_size: LotSize

    @model_validator(mode="after")
    def _quantity_is_a_whole_non_zero_position(self) -> ApprovedLeg:
        if self.lots.count == 0:
            raise ValueError(f"leg {self.leg_id} was approved with zero lots")
        return self

    @property
    def quantity(self) -> Quantity:
        return self.lots.to_quantity(self.lot_size)


class RiskDecision(VersionedModel):
    """The gate every intent must pass, with machine-readable reasons."""

    decision_id: NonEmptyStr
    intent_id: NonEmptyStr
    correlation_id: NonEmptyStr
    policy_version: NonEmptyStr
    config_version: NonEmptyStr
    action: RiskAction
    approved_legs: tuple[ApprovedLeg, ...] = ()
    capital_reservation_id: NonEmptyStr | None = None
    reserved_capital: Money | None = None
    recalculated_max_loss: Money | None = None
    margin_required: Money | None = None
    pre_trade_exposure: ExposureSnapshot
    post_trade_projection: ExposureSnapshot | None = None
    applied_limits: tuple[NonEmptyStr, ...] = ()
    reason_codes: tuple[ReasonCode, ...]
    liquidity_note: NonEmptyStr | None = None
    decided_at: UtcDatetime
    expires_at: UtcDatetime
    attempt_budget: StrictInt = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _outcome_matches_its_evidence(self) -> RiskDecision:
        if self.expires_at <= self.decided_at:
            raise ValueError(
                "expires_at must be after decided_at; a decision with no lifetime "
                "cannot be safely acted on"
            )
        if not self.reason_codes:
            raise ValueError(
                "every decision records at least one reason code so alerting and "
                "promotion gates can route it without parsing prose"
            )

        approving = self.action in _APPROVING_ACTIONS
        if approving:
            self._validate_approval()
        else:
            self._validate_refusal()
        return self

    def _validate_approval(self) -> None:
        if not self.approved_legs:
            raise ValueError(f"{self.action} must specify approved legs")
        if self.capital_reservation_id is None or self.reserved_capital is None:
            raise ValueError(
                f"{self.action} requires a capital reservation before submission "
                "(invariant 14)"
            )
        if self.reserved_capital.is_negative or self.reserved_capital.is_zero:
            raise ValueError("reserved capital must be positive")
        if self.recalculated_max_loss is None or self.margin_required is None:
            raise ValueError(
                f"{self.action} requires Layer 2's own max-loss and margin figures; "
                "the strategy's estimate is not authoritative (invariant 4)"
            )
        if self.recalculated_max_loss.is_negative or self.recalculated_max_loss.is_zero:
            raise ValueError("recalculated_max_loss must be positive")
        if self.post_trade_projection is None:
            raise ValueError(f"{self.action} requires a projected post-fill state")
        leg_ids = [leg.leg_id for leg in self.approved_legs]
        if len(set(leg_ids)) != len(leg_ids):
            raise ValueError("approved leg_id values must be unique")

    def _validate_refusal(self) -> None:
        if self.approved_legs:
            raise ValueError(f"{self.action} must not approve any leg")
        if self.capital_reservation_id is not None or self.reserved_capital is not None:
            raise ValueError(
                f"{self.action} must not hold a capital reservation; unreleased "
                "capital on a refused intent silently shrinks available margin"
            )
        if all(code is ReasonCode.OK for code in self.reason_codes):
            raise ValueError(
                f"{self.action} cannot be explained by OK alone; record the limit "
                "or condition that caused it"
            )

    @property
    def permits_submission(self) -> bool:
        return self.action in _APPROVING_ACTIONS

    def is_valid_at(self, now: datetime) -> bool:
        """An expired approval must be recalculated, never consumed."""
        return self.permits_submission and self.decided_at <= now < self.expires_at
