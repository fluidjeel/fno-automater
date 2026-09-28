"""Shared view assembly: probability to direction, sigma and event levels."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from trading.config.forecast import ForecastConfig, ModeForecastConfig
from trading.domain.contracts.forecast import ForecastEventKind
from trading.domain.enums import FamilyId, ModeId, ReasonCode
from trading.forecast.bars import Bar, daily_bars
from trading.forecast.distribution import (
    HorizonDistribution,
    distribution_from_direction,
)
from trading.forecast.inputs import ForecastInputs, ModeView, event_for, horizon_years
from trading.forecast.logistic import logistic_probability
from trading.forecast.volatility import (
    HarCoefficients,
    daily_realized_variance,
    har_variance_forecast,
    intraday_bar_sigma,
)

__all__ = [
    "assemble_view",
    "completed_daily_bars",
    "daily_variance_forecast",
    "direction_from_probability",
    "event_probability",
    "har_coefficients",
    "horizon_sigma",
    "score_model",
]

_ZERO = Decimal(0)
_HALF = Decimal("0.5")
_SIGMA_FLOOR = Decimal("0.0001")


def direction_from_probability(p_up: Decimal, margin: Decimal) -> int:
    """+1, -1, or 0 inside the no-view band around 50%."""
    if p_up >= _HALF + margin:
        return 1
    if p_up <= _HALF - margin:
        return -1
    return 0


def score_model(
    mode: ModeForecastConfig, features: Mapping[str, Decimal]
) -> tuple[Decimal, tuple[str, ...]]:
    """Directional probability from the mode's frozen logistic model."""
    return logistic_probability(
        intercept=mode.model.intercept,
        coefficients=mode.model.coefficients,
        features=features,
    )


def har_coefficients(config: ForecastConfig) -> HarCoefficients:
    """The single daily-variance model every horizon above intraday shares."""
    har = config.m4.har
    return HarCoefficients(
        intercept=har.intercept, daily=har.daily, weekly=har.weekly, monthly=har.monthly
    )


def completed_daily_bars(inputs: ForecastInputs) -> tuple[Bar, ...]:
    """Daily bars excluding the in-progress session."""
    rows = daily_bars(inputs.bars, inputs.zone)
    today = inputs.as_of.astimezone(inputs.zone).date()
    if rows and rows[-1].start.astimezone(inputs.zone).date() == today:
        return rows[:-1]
    return rows


def daily_variance_forecast(
    inputs: ForecastInputs, har: HarCoefficients
) -> Decimal | None:
    """HAR next-session variance from intraday bars (overnight included)."""
    rows = daily_realized_variance(inputs.bars, inputs.zone)
    # The current session is incomplete, so it would understate variance.
    if rows and rows[-1][0] == inputs.as_of.astimezone(inputs.zone).date():
        rows = rows[:-1]
    return har_variance_forecast([value for _day, value in rows], har)


def horizon_sigma(
    config: ForecastConfig,
    mode: ModeForecastConfig,
    *,
    bars: Sequence[Bar],
    daily_variance: Decimal | None,
    implied_vol: Decimal | None,
    intraday_weight: Decimal,
) -> tuple[Decimal | None, tuple[str, ...]]:
    """Blend intraday and daily realized estimates; fall back to implied vol."""
    absent: list[str] = []
    bar_minutes = Decimal(config.bar_seconds) / Decimal(60)
    horizon_bars = Decimal(mode.horizon_minutes) / bar_minutes
    per_bar = intraday_bar_sigma(bars, window=50)
    intraday = None if per_bar is None else per_bar * horizon_bars.sqrt()
    if intraday is None:
        absent.append("intraday_realized_vol")
    daily = None
    if daily_variance is not None and daily_variance > 0:
        daily = (
            daily_variance
            * Decimal(mode.horizon_minutes)
            / Decimal(config.session_minutes)
        ).sqrt()
    else:
        absent.append("daily_realized_vol")
    sigma: Decimal | None
    if intraday is not None and daily is not None:
        sigma = intraday_weight * intraday + (1 - intraday_weight) * daily
    else:
        sigma = intraday if intraday is not None else daily
    if sigma is None and implied_vol is not None:
        sigma = implied_vol * horizon_years(config, mode).sqrt()
        absent.append("realized_vol_fallback_implied")
    if sigma is not None:
        sigma = max(sigma, _SIGMA_FLOOR)
    return sigma, tuple(absent)


def event_probability(
    distribution: HorizonDistribution, event: ForecastEventKind, threshold: Decimal
) -> Decimal:
    """P(event) under a horizon distribution; threshold is a log return."""
    if event is ForecastEventKind.UP_MOVE:
        return distribution.prob_above(threshold)
    if event is ForecastEventKind.DOWN_MOVE:
        return distribution.prob_below(-threshold)
    inside = distribution.prob_between(-threshold, threshold)
    if event is ForecastEventKind.LEAVE_BAND:
        return 1 - inside
    return inside


def assemble_view(
    *,
    mode_id: ModeId,
    config: ForecastConfig,
    mode: ModeForecastConfig,
    p_up: Decimal,
    sigma: Decimal,
    implied_vol: Decimal | None,
    features: dict[str, Decimal],
    absent: Sequence[str],
    reasons: Sequence[ReasonCode] = (),
    neutral_view: bool = False,
    event_override: ForecastEventKind | None = None,
    invalidation_below: Decimal | None = None,
    invalidation_above: Decimal | None = None,
    view: str | None = None,
    allowed_families: frozenset[FamilyId] | None = None,
) -> ModeView:
    """Map probability and sigma into the mode's forecast distribution and event."""
    direction = direction_from_probability(p_up, mode.min_directional_margin)
    if neutral_view:
        direction = 0
    event = event_override or event_for(direction)
    return ModeView(
        mode_id=mode_id,
        p_up=p_up,
        direction=direction,
        distribution=distribution_from_direction(p_up, sigma),
        horizon_years=horizon_years(config, mode),
        event=event,
        event_threshold=mode.event_threshold_sigma * sigma,
        target_move_fraction=mode.target_sigma * sigma,
        stop_move_fraction=mode.stop_sigma * sigma,
        implied_vol=implied_vol,
        features=features,
        absent=tuple(dict.fromkeys(absent)),
        reason_codes=tuple(dict.fromkeys(reasons)),
        invalidation_below=invalidation_below,
        invalidation_above=invalidation_above,
        view=view,
        allowed_families=allowed_families,
    )
