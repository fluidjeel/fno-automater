"""Layer 4 horizon labels for ModeForecast records.

Labels are computed after the fact from underlying bars and never touch live
state. The horizon is counted in session bars, so a five-day M3 horizon spans
five sessions regardless of nights, weekends or holidays.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal

from trading.domain.contracts.forecast import (
    ForecastBarrier,
    ForecastEventKind,
    ForecastLabel,
    ModeForecast,
)
from trading.forecast.bars import Bar, parse_bars
from trading.forecast.distribution import structure_value

__all__ = [
    "LabelTiming",
    "bars_from_payloads",
    "horizon_bar_count",
    "label_forecast",
    "label_forecasts",
    "split_forecast_records",
]

_ONE = Decimal(1)
_QUANT = Decimal("0.00000001")
_CALENDAR_SLACK_DAYS = 3


@dataclass(frozen=True, slots=True)
class LabelTiming:
    """Bar cadence and trading-year convention the forecasts were made under."""

    bar_seconds: int
    session_minutes: int
    annual_trading_days: int


def horizon_bar_count(forecast: ModeForecast, bar_seconds: int) -> int:
    """Number of session bars that make up the forecast's horizon."""
    return max(1, forecast.horizon_seconds // bar_seconds)


def _stale_after(forecast: ModeForecast, timing: LabelTiming) -> timedelta:
    """Wall-clock allowance after which an incomplete path is labelled as-is."""
    sessions = -(-forecast.horizon_seconds // (timing.session_minutes * 60))
    return timedelta(days=2 * sessions + _CALENDAR_SLACK_DAYS)


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANT, rounding=ROUND_HALF_EVEN)


def _levels(forecast: ModeForecast) -> tuple[Decimal, Decimal]:
    """Upper and lower barrier prices implied by the forecast's event."""
    spot = forecast.spot
    if forecast.event in {ForecastEventKind.STAY_IN_BAND, ForecastEventKind.LEAVE_BAND}:
        band = forecast.event_threshold
        return spot * band.exp(), spot * (-band).exp()
    if forecast.event is ForecastEventKind.DOWN_MOVE:
        return (
            spot * (_ONE + forecast.stop_move_fraction),
            spot * (_ONE - forecast.target_move_fraction),
        )
    return (
        spot * (_ONE + forecast.target_move_fraction),
        spot * (_ONE - forecast.stop_move_fraction),
    )


def _first_touch(
    forecast: ModeForecast, path: Sequence[Bar]
) -> tuple[ForecastBarrier, datetime | None]:
    """Triple-barrier outcome; a bar touching both sides counts against the view."""
    upper, lower = _levels(forecast)
    event = forecast.event
    for bar in path:
        up, down = bar.high >= upper, bar.low <= lower
        if not (up or down):
            continue
        if event is ForecastEventKind.STAY_IN_BAND:
            return ForecastBarrier.STOP, bar.start
        if event is ForecastEventKind.LEAVE_BAND:
            return ForecastBarrier.TARGET, bar.start
        favourable = up if event is ForecastEventKind.UP_MOVE else down
        adverse = down if event is ForecastEventKind.UP_MOVE else up
        if adverse:
            return ForecastBarrier.STOP, bar.start
        if favourable:
            return ForecastBarrier.TARGET, bar.start
    return ForecastBarrier.TIMEOUT, None


def _excursions(forecast: ModeForecast, path: Sequence[Bar]) -> tuple[Decimal, Decimal]:
    """Max favourable and max adverse move as signed fractions of entry spot.

    Directional views measure in the thesis direction; band views report the
    raw upside as favourable and the raw downside as adverse.
    """
    spot = forecast.spot
    high = max(bar.high for bar in path) / spot - _ONE
    low = min(bar.low for bar in path) / spot - _ONE
    if forecast.event is ForecastEventKind.DOWN_MOVE:
        return -low, -high
    return high, low


def _event_occurred(forecast: ModeForecast, realized: Decimal) -> bool:
    threshold = forecast.event_threshold
    if forecast.event is ForecastEventKind.UP_MOVE:
        return realized >= threshold
    if forecast.event is ForecastEventKind.DOWN_MOVE:
        return realized <= -threshold
    if forecast.event is ForecastEventKind.STAY_IN_BAND:
        return abs(realized) < threshold
    return abs(realized) >= threshold


def _realized_structure(
    forecast: ModeForecast, end_spot: Decimal, timing: LabelTiming
) -> tuple[Decimal | None, Decimal | None]:
    """Hold-to-horizon mark of the priced structure with frozen entry IVs."""
    if not forecast.legs or forecast.structure_cost is None:
        return None, None
    horizon_years = Decimal(forecast.horizon_seconds) / Decimal(
        60 * timing.session_minutes * timing.annual_trading_days
    )
    elapsed = min(horizon_years, max(leg.expiry_years for leg in forecast.legs))
    fallback = (
        forecast.implied_sigma / horizon_years.sqrt()
        if forecast.implied_sigma is not None and horizon_years > 0
        else forecast.forecast_sigma / horizon_years.sqrt()
    )
    value = structure_value(
        forecast.legs, end_spot, elapsed_years=elapsed, fallback_vol=fallback
    )
    edge = value - forecast.structure_cost - forecast.cost_per_unit
    return _q(value), _q(edge)


def label_forecast(
    forecast: ModeForecast,
    bars: Sequence[Bar],
    *,
    timing: LabelTiming,
    labelled_at: datetime,
    label_id: str,
) -> ForecastLabel | None:
    """Label one forecast once its horizon has elapsed, else ``None``.

    ``bars`` must be completed underlying bars sorted by start. A path that is
    still short after a generous calendar allowance is labelled incomplete so
    data gaps surface instead of silently shrinking the sample.
    """
    needed = horizon_bar_count(forecast, timing.bar_seconds)
    path = [bar for bar in bars if bar.start >= forecast.as_of][:needed]
    complete = len(path) >= needed
    if not path:
        return None
    if not complete and labelled_at < forecast.as_of + _stale_after(forecast, timing):
        return None
    end = path[-1]
    realized = _q((end.close / forecast.spot).ln())
    barrier, barrier_at = _first_touch(forecast, path)
    favourable, adverse = _excursions(forecast, path)
    value, edge = _realized_structure(forecast, end.close, timing)
    return ForecastLabel(
        label_id=label_id,
        forecast_id=forecast.forecast_id,
        mode_id=forecast.mode_id,
        family_id=forecast.family_id,
        labelled_at=labelled_at,
        horizon_end=end.start + timedelta(seconds=timing.bar_seconds),
        complete=complete,
        barrier=barrier,
        barrier_at=barrier_at,
        event_occurred=_event_occurred(forecast, realized),
        realized_return=realized,
        max_favorable=_q(favourable),
        max_adverse=_q(adverse),
        realized_structure_value=value,
        realized_edge=edge,
        bars_used=len(path),
    )


def label_forecasts(
    forecasts: Iterable[ModeForecast],
    bars: Sequence[Bar],
    *,
    timing: LabelTiming,
    labelled_at: datetime,
    new_id: Callable[[str], str],
    already_labelled: frozenset[str] = frozenset(),
) -> tuple[ForecastLabel, ...]:
    """Label every due forecast that does not have a label yet."""
    ordered = sorted(bars, key=lambda bar: bar.start)
    labels: list[ForecastLabel] = []
    for forecast in forecasts:
        if forecast.forecast_id in already_labelled:
            continue
        label = label_forecast(
            forecast,
            ordered,
            timing=timing,
            labelled_at=labelled_at,
            label_id=new_id("FLB"),
        )
        if label is not None:
            labels.append(label)
    return tuple(labels)


def bars_from_payloads(
    payloads: Iterable[Mapping[str, object]], *, bar_seconds: int, as_of: datetime
) -> tuple[Bar, ...]:
    """Union of BAR_SNAPSHOT payload bars, one bar per start time (latest wins)."""
    merged: dict[datetime, Bar] = {}
    for payload in payloads:
        rows = payload.get("bars")
        if not isinstance(rows, list):
            continue
        for bar in parse_bars(rows, bar_seconds=bar_seconds, as_of=as_of):
            merged[bar.start] = bar
    return tuple(merged[key] for key in sorted(merged))


def split_forecast_records(
    payloads: Iterable[object],
) -> tuple[tuple[ModeForecast, ...], tuple[ForecastLabel, ...]]:
    """Forecasts and labels from a replayed ledger, other records ignored."""
    forecasts: list[ModeForecast] = []
    labels: list[ForecastLabel] = []
    for payload in payloads:
        if isinstance(payload, ModeForecast):
            forecasts.append(payload)
        elif isinstance(payload, ForecastLabel):
            labels.append(payload)
    return tuple(forecasts), tuple(labels)
