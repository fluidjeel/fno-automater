"""Portfolio exposure arithmetic for Agent Desk PORTFOLIO (ADESK-A4).

Deterministic only: no LLM. Built before any desk agent sees the book.
Hard limits live in risk.yaml and are enforced via evaluate_exposure_limits.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.primitives import Money

__all__ = [
    "CorrelationPair",
    "EventOverlap",
    "ExposureReport",
    "NotionalBucket",
]


class NotionalBucket(StrictModel):
    """Named notional aggregate (underlying, sector, or expiry date)."""

    key: NonEmptyStr
    notional: Money


class CorrelationPair(StrictModel):
    """Pairwise return correlation of two underlyings over a declared window."""

    underlying_a: NonEmptyStr
    underlying_b: NonEmptyStr
    correlation: ExactDecimal = Field(ge=Decimal("-1"), le=Decimal("1"))
    window: NonEmptyStr


class EventOverlap(StrictModel):
    """Open positions that share one scheduled macro / thesis event."""

    event_id: NonEmptyStr
    position_count: StrictInt = Field(ge=0)
    notional: Money


class ExposureReport(VersionedModel):
    """Book-level Greeks and shared-fate arithmetic. Index-equivalent units."""

    as_of: UtcDatetime
    beta_version: NonEmptyStr
    correlation_window: NonEmptyStr
    open_position_count: StrictInt = Field(ge=0)
    equity: Money
    net_delta: ExactDecimal
    net_vega: ExactDecimal
    net_theta: ExactDecimal
    net_gamma: ExactDecimal
    beta_weighted_delta_nifty: ExactDecimal
    notional_by_underlying: tuple[NotionalBucket, ...] = ()
    notional_by_sector: tuple[NotionalBucket, ...] = ()
    notional_by_expiry: tuple[NotionalBucket, ...] = ()
    pairwise_correlations: tuple[CorrelationPair, ...] = ()
    event_overlaps: tuple[EventOverlap, ...] = ()
    # Fraction of open risk pointing the same way: |sum signed| / sum |signed|.
    # Empty book is 0.
    directional_agreement_ratio: ExactDecimal = Field(ge=Decimal(0), le=Decimal(1))

    @model_validator(mode="after")
    def _one_currency(self) -> ExposureReport:
        currencies = {self.equity.currency}
        for bucket in (
            self.notional_by_underlying
            + self.notional_by_sector
            + self.notional_by_expiry
        ):
            currencies.add(bucket.notional.currency)
        for overlap in self.event_overlaps:
            currencies.add(overlap.notional.currency)
        if len(currencies) != 1:
            raise ValueError(
                f"ExposureReport mixes currencies "
                f"{sorted(c.value for c in currencies)}; convert before aggregating"
            )
        return self
