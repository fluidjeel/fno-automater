"""Sizing request and decision contracts.

Layer 2 recalculates lots from risk, capital, margin, portfolio limits and
liquidity. The binding constraint records which term in the min() bound the size.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.intent import TradeIntent
from trading.domain.contracts.portfolio import PortfolioSnapshot
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import SizingBindingConstraint
from trading.domain.primitives import Lots, LotSize, Money

__all__ = ["SizingDecision", "SizingLegResult", "SizingLimits", "SizingRequest"]


class SizingLimits(StrictModel):
    """Snapshot of the limit inputs used for one sizing calculation."""

    policy_version: NonEmptyStr
    config_version: NonEmptyStr
    max_loss_per_trade: Money
    daily_loss_remaining: Money
    strategy_allocation_remaining: Money
    margin_available: Money


class SizingRequest(VersionedModel):
    """Everything needed to size one intent deterministically."""

    request_id: NonEmptyStr
    intent: TradeIntent
    feature_snapshot: FeatureSnapshot
    portfolio_snapshot: PortfolioSnapshot
    limits: SizingLimits
    requested_at: UtcDatetime

    @model_validator(mode="after")
    def _snapshots_align_with_intent(self) -> SizingRequest:
        if self.intent.snapshot_id != self.feature_snapshot.snapshot_id:
            raise ValueError(
                "feature_snapshot_id must match intent.snapshot_id for replay lineage"
            )
        if self.intent.underlying != self.feature_snapshot.contract.underlying:
            raise ValueError("feature snapshot underlying must match intent")
        return self


class SizingLegResult(StrictModel):
    """Computed lots for one intent leg."""

    leg_id: NonEmptyStr
    lots: Lots
    lot_size: LotSize

    @model_validator(mode="after")
    def _lots_are_positive(self) -> SizingLegResult:
        if self.lots.count == 0:
            raise ValueError(f"leg {self.leg_id} was sized to zero lots")
        return self


class SizingDecision(VersionedModel):
    """Outcome of the min() sizing formula for one intent."""

    sizing_id: NonEmptyStr
    request_id: NonEmptyStr
    intent_id: NonEmptyStr
    binding_constraint: SizingBindingConstraint
    approved_legs: tuple[SizingLegResult, ...]
    risk_lots: StrictInt = Field(ge=0)
    capital_lots: StrictInt = Field(ge=0)
    margin_lots: StrictInt = Field(ge=0)
    portfolio_limit_lots: StrictInt = Field(ge=0)
    liquidity_lots: StrictInt = Field(ge=0)
    estimated_margin: Money
    recalculated_max_loss: Money
    decided_at: UtcDatetime

    @model_validator(mode="after")
    def _min_terms_and_legs_agree(self) -> SizingDecision:
        if not self.approved_legs:
            raise ValueError("sizing decision must approve at least one leg")
        binding_lots = {
            SizingBindingConstraint.RISK: self.risk_lots,
            SizingBindingConstraint.CAPITAL: self.capital_lots,
            SizingBindingConstraint.MARGIN: self.margin_lots,
            SizingBindingConstraint.PORTFOLIO_LIMIT: self.portfolio_limit_lots,
            SizingBindingConstraint.LIQUIDITY: self.liquidity_lots,
        }[self.binding_constraint]
        for leg in self.approved_legs:
            if leg.lots.count > binding_lots:
                raise ValueError(
                    f"leg {leg.leg_id} has {leg.lots.count} lots but binding "
                    f"constraint {self.binding_constraint} allows {binding_lots}"
                )
        leg_ids = [leg.leg_id for leg in self.approved_legs]
        if len(set(leg_ids)) != len(leg_ids):
            raise ValueError("approved leg_id values must be unique")
        if self.recalculated_max_loss.is_negative or self.recalculated_max_loss.is_zero:
            raise ValueError("recalculated_max_loss must be positive")
        if self.estimated_margin.is_negative:
            raise ValueError("estimated_margin must not be negative")
        return self

    @property
    def approved_lots(self) -> int:
        return self.approved_legs[0].lots.count
