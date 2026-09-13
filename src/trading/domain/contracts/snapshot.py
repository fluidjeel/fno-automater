"""FeatureSnapshot: the immutable market state one decision is made against.

Invariant 18: a decision references one immutable versioned snapshot, and mixed
timestamps are never hidden. Every time field is therefore explicit and separate,
rather than collapsed into a single "timestamp".
"""

from __future__ import annotations

from datetime import datetime, timedelta

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.common import ContractRef, DataQualityReport, Lineage
from trading.domain.enums import InstrumentKind, OptionType
from trading.domain.primitives import Price

__all__ = [
    "DerivativesContext",
    "FeatureSnapshot",
    "Greeks",
    "MarketQuote",
    "SnapshotTimes",
]


class SnapshotTimes(StrictModel):
    """The four distinct instants behind one snapshot.

    They are separate fields because conflating them is how lookahead enters a
    backtest: event_time is when the market acted, calculation_time is when we
    reacted, and the gap between them is the latency a replay must reproduce.
    """

    event_time: UtcDatetime
    source_time: UtcDatetime
    receive_time: UtcDatetime
    calculation_time: UtcDatetime

    @model_validator(mode="after")
    def _causality_holds(self) -> SnapshotTimes:
        if self.receive_time < self.event_time:
            raise ValueError(
                "receive_time precedes event_time; the feed cannot deliver an "
                "event before it happened, so this indicates clock skew"
            )
        if self.calculation_time < self.receive_time:
            raise ValueError("calculation_time precedes receive_time")
        return self

    def age_at(self, now: datetime) -> timedelta:
        """How stale this snapshot is relative to an injected clock reading."""
        return now - self.event_time


class MarketQuote(StrictModel):
    """Top-of-book and bar state. Depth detail stays summarized here."""

    bid: Price | None = None
    ask: Price | None = None
    last: Price | None = None
    open: Price | None = None
    high: Price | None = None
    low: Price | None = None
    close: Price | None = None
    volume: StrictInt | None = Field(default=None, ge=0)
    bid_size: StrictInt | None = Field(default=None, ge=0)
    ask_size: StrictInt | None = Field(default=None, ge=0)
    bar_is_final: bool = False

    @model_validator(mode="after")
    def _book_is_not_crossed(self) -> MarketQuote:
        """A crossed book is a data-quality failure, not a tradable quote."""
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError(
                f"crossed book: bid {self.bid} exceeds ask {self.ask}; the quality "
                "gate must mark this INVALID rather than pricing against it"
            )
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError(f"high {self.high} is below low {self.low}")
        return self

    @property
    def spread(self) -> ExactDecimal | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask.value - self.bid.value


class Greeks(StrictModel):
    """Model output, carrying the model identity that produced it."""

    model: NonEmptyStr
    calculation_version: NonEmptyStr
    converged: bool
    implied_volatility: ExactDecimal | None = None
    delta: ExactDecimal | None = None
    gamma: ExactDecimal | None = None
    theta: ExactDecimal | None = None
    vega: ExactDecimal | None = None
    rho: ExactDecimal | None = None

    @model_validator(mode="after")
    def _non_converged_output_is_empty(self) -> Greeks:
        """A non-converged solve must not present values that look usable."""
        if not self.converged and any(
            value is not None
            for value in (
                self.implied_volatility,
                self.delta,
                self.gamma,
                self.theta,
                self.vega,
                self.rho,
            )
        ):
            raise ValueError(
                "a non-converged calculation must not publish Greeks; report the "
                "failure so the quality gate can degrade the snapshot"
            )
        if self.implied_volatility is not None and self.implied_volatility < 0:
            raise ValueError("implied volatility must not be negative")
        return self


class DerivativesContext(StrictModel):
    """Option and futures specifics for a derivative snapshot."""

    days_to_expiry: StrictInt = Field(ge=0)
    open_interest: StrictInt | None = Field(default=None, ge=0)
    option_type: OptionType | None = None
    greeks: Greeks | None = None
    underlying_price: Price | None = None


class FeatureSnapshot(VersionedModel):
    """Immutable, versioned market state for exactly one decision."""

    snapshot_id: NonEmptyStr
    contract: ContractRef
    times: SnapshotTimes
    market: MarketQuote
    derivatives: DerivativesContext | None = None
    feature_set_version: NonEmptyStr
    features: dict[NonEmptyStr, ExactDecimal] = Field(default_factory=dict)
    quality: DataQualityReport
    lineage: Lineage

    @model_validator(mode="after")
    def _derivative_snapshots_carry_derivative_context(self) -> FeatureSnapshot:
        needs_context = self.contract.instrument_kind in {
            InstrumentKind.OPTION,
            InstrumentKind.FUTURE,
        }
        if needs_context and self.derivatives is None:
            raise ValueError(
                f"{self.contract.instrument_kind} snapshot requires a "
                "DerivativesContext; expiry-aware logic depends on it"
            )
        if not needs_context and self.derivatives is not None:
            raise ValueError(
                f"{self.contract.instrument_kind} snapshot must not carry a "
                "DerivativesContext"
            )
        return self

    @property
    def permits_new_exposure(self) -> bool:
        """Invariant 6, delegated to the quality gate."""
        return self.quality.permits_new_exposure
