"""Inputs every mode forecaster reads and the view each one returns."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading.config.forecast import ForecastConfig, ModeForecastConfig
from trading.domain.contracts import MarketState
from trading.domain.contracts.forecast import ForecastEventKind
from trading.domain.enums import FamilyId, ModeId, ReasonCode
from trading.forecast.bars import Bar
from trading.forecast.distribution import HorizonDistribution
from trading.forecast.reference import ReferenceInputs

__all__ = [
    "ForecastInputs",
    "ModeView",
    "OrderFlowStats",
    "PositioningStats",
    "annual_vol",
    "clamp",
    "event_for",
    "horizon_years",
    "sign",
]

_ZERO = Decimal(0)
_ONE = Decimal(1)
_PERCENT_CUTOFF = Decimal(3)
_HUNDRED = Decimal(100)


@dataclass(frozen=True, slots=True)
class OrderFlowStats:
    """Call-minus-put order flow over two rolling windows."""

    ofi_short: Decimal | None
    ofi_long: Decimal | None
    microprice_drift_bps: Decimal | None
    samples: int


@dataclass(frozen=True, slots=True)
class PositioningStats:
    """Session-relative option positioning."""

    pcr: Decimal | None
    pcr_change: Decimal | None
    oi_wall_shift: Decimal | None


@dataclass(frozen=True, slots=True)
class ForecastInputs:
    """Everything a forecaster may read for one decision instant."""

    as_of: datetime
    zone: ZoneInfo
    spot: Decimal
    bars: tuple[Bar, ...]
    market: MarketState | None = None
    current_vix: Decimal | None = None
    previous_vix: Decimal | None = None
    iv_percentile: Decimal | None = None
    atm_iv_by_expiry: tuple[tuple[date, Decimal], ...] = ()
    reference: ReferenceInputs = field(default_factory=ReferenceInputs)
    positioning: PositioningStats | None = None
    order_flow: OrderFlowStats | None = None
    macro_bias: Decimal | None = None
    underlying_features: dict[str, Decimal] = field(default_factory=dict)
    expiry_today: bool = False


@dataclass(frozen=True, slots=True)
class ModeView:
    """A mode's directional and volatility view before any structure is priced."""

    mode_id: ModeId
    p_up: Decimal
    direction: int
    distribution: HorizonDistribution
    horizon_years: Decimal
    event: ForecastEventKind
    event_threshold: Decimal
    target_move_fraction: Decimal
    stop_move_fraction: Decimal
    implied_vol: Decimal | None
    features: dict[str, Decimal]
    absent: tuple[str, ...]
    reason_codes: tuple[ReasonCode, ...] = ()
    invalidation_below: Decimal | None = None
    invalidation_above: Decimal | None = None
    view: str | None = None
    allowed_families: frozenset[FamilyId] | None = None


def clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    """Bound ``value`` to ``[low, high]``."""
    return min(max(value, low), high)


def sign(value: Decimal | None) -> Decimal:
    """-1, 0 or 1."""
    if value is None or value == 0:
        return _ZERO
    return _ONE if value > 0 else -_ONE


def annual_vol(value: Decimal | None) -> Decimal | None:
    """Annualized vol as a fraction; percent quotes (VIX 14.2, IV 12.5) are scaled.

    No listed index option trades at a 300% annual vol, so anything above 3 is
    a percentage quote.
    """
    if value is None or value <= 0:
        return None
    return value / _HUNDRED if value > _PERCENT_CUTOFF else value


def horizon_years(config: ForecastConfig, mode: ModeForecastConfig) -> Decimal:
    """Trading-time horizon in years."""
    return Decimal(mode.horizon_minutes) / Decimal(
        config.annual_trading_days * config.session_minutes
    )


def event_for(direction: int) -> ForecastEventKind:
    """The terminal event a directional or neutral view predicts."""
    if direction > 0:
        return ForecastEventKind.UP_MOVE
    if direction < 0:
        return ForecastEventKind.DOWN_MOVE
    return ForecastEventKind.STAY_IN_BAND
