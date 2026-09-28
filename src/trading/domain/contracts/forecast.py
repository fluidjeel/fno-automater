"""Per-mode forecast records and their horizon outcome labels.

A ``ModeForecast`` is what a mode believed at decision time: a directional
probability, a log-return distribution over the mode's own horizon, and, when a
structure was bound, that structure's modelled value versus its market cost.
Labels are written later by Layer 4 and never mutate the forecast.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum, unique

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import ModeId, OptionType, ReasonCode, Side

__all__ = [
    "ForecastBarrier",
    "ForecastEventKind",
    "ForecastLabel",
    "ForecastLeg",
    "ModeForecast",
]


@unique
class ForecastEventKind(StrEnum):
    """The terminal event whose probability ``p_event`` states."""

    UP_MOVE = "UP_MOVE"
    DOWN_MOVE = "DOWN_MOVE"
    STAY_IN_BAND = "STAY_IN_BAND"
    LEAVE_BAND = "LEAVE_BAND"


@unique
class ForecastBarrier(StrEnum):
    """Which triple barrier the underlying touched first."""

    TARGET = "TARGET"
    STOP = "STOP"
    TIMEOUT = "TIMEOUT"


class ForecastLeg(VersionedModel):
    """One option leg of the structure a forecast priced."""

    symbol: NonEmptyStr
    option_type: OptionType
    strike: ExactDecimal = Field(gt=0)
    side: Side
    ratio: StrictInt = Field(ge=1)
    expiry_years: ExactDecimal = Field(ge=0)
    mid: ExactDecimal = Field(ge=0)
    implied_volatility: ExactDecimal | None = Field(default=None, gt=0)


class ModeForecast(VersionedModel):
    """One mode's forecast at one decision instant, traded or not."""

    forecast_id: NonEmptyStr
    cycle_id: NonEmptyStr
    as_of: UtcDatetime
    mode_id: ModeId
    family_id: NonEmptyStr | None = None
    model_version: NonEmptyStr
    config_version: NonEmptyStr
    horizon_seconds: StrictInt = Field(gt=0)
    spot: ExactDecimal = Field(gt=0)
    direction: StrictInt = Field(ge=-1, le=1)
    p_up: ExactDecimal = Field(ge=0, le=1)
    event: ForecastEventKind
    event_threshold: ExactDecimal = Field(gt=0)
    p_event: ExactDecimal = Field(ge=0, le=1)
    p_event_implied: ExactDecimal | None = Field(default=None, ge=0, le=1)
    forecast_drift: ExactDecimal
    forecast_sigma: ExactDecimal = Field(gt=0)
    implied_sigma: ExactDecimal | None = Field(default=None, gt=0)
    target_move_fraction: ExactDecimal = Field(gt=0)
    stop_move_fraction: ExactDecimal = Field(gt=0)
    legs: tuple[ForecastLeg, ...] = ()
    structure_cost: ExactDecimal | None = None
    cost_per_unit: ExactDecimal = Field(default=Decimal(0), ge=0)
    forecast_value: ExactDecimal | None = None
    implied_value: ExactDecimal | None = None
    edge_after_costs: ExactDecimal | None = None
    p_profit: ExactDecimal | None = Field(default=None, ge=0, le=1)
    p_profit_implied: ExactDecimal | None = Field(default=None, ge=0, le=1)
    invalidation_below: ExactDecimal | None = Field(default=None, gt=0)
    invalidation_above: ExactDecimal | None = Field(default=None, gt=0)
    view: NonEmptyStr | None = None
    gate_passed: StrictBool
    gate_enforced: StrictBool
    reason_codes: tuple[ReasonCode, ...] = ()
    features: dict[NonEmptyStr, ExactDecimal] = Field(default_factory=dict)
    absent_features: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def _structure_fields_travel_together(self) -> ModeForecast:
        priced = (self.structure_cost, self.forecast_value, self.edge_after_costs)
        if self.legs and any(item is None for item in priced):
            raise ValueError("a priced structure needs cost, value and edge")
        if not self.legs and any(item is not None for item in priced):
            raise ValueError("structure pricing requires legs")
        return self


class ForecastLabel(VersionedModel):
    """Realized outcome of one forecast at its horizon."""

    label_id: NonEmptyStr
    forecast_id: NonEmptyStr
    mode_id: ModeId
    family_id: NonEmptyStr | None = None
    labelled_at: UtcDatetime
    horizon_end: UtcDatetime
    complete: StrictBool
    barrier: ForecastBarrier
    barrier_at: UtcDatetime | None = None
    event_occurred: StrictBool
    realized_return: ExactDecimal
    max_favorable: ExactDecimal
    max_adverse: ExactDecimal
    realized_structure_value: ExactDecimal | None = None
    realized_edge: ExactDecimal | None = None
    bars_used: StrictInt = Field(ge=0)
