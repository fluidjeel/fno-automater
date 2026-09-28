"""M4 strategic forecaster: a weekly regime of direction, volatility and events.

Price, volatility, flows, global drivers and the event calendar are combined
into one weekly state. It is held for the ISO week and rebuilt only on a new
week or a shock, so a weeks-horizon book is not re-decided on intraday noise.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from trading.config.forecast import ForecastConfig
from trading.domain.contracts.forecast import ForecastEventKind
from trading.domain.enums import FamilyId, ModeId, ReasonCode
from trading.forecast.bars import atr, sma
from trading.forecast.common import (
    assemble_view,
    completed_daily_bars,
    daily_variance_forecast,
    direction_from_probability,
    har_coefficients,
    score_model,
)
from trading.forecast.inputs import ForecastInputs, ModeView, annual_vol, clamp
from trading.forecast.m3 import fii_positioning
from trading.forecast.reference import CalendarEvent, GlobalMacroRow, events_between
from trading.forecast.volatility import log_returns, stdev

__all__ = [
    "M4_LONG_VOL",
    "M4_SHORT_VOL",
    "WeeklyRegime",
    "m4_regime",
    "m4_regime_is_stale",
    "m4_view",
]

_ZERO = Decimal(0)
_ONE = Decimal(1)
_HALF = Decimal("0.5")
_THREE = Decimal(3)
_CALENDAR_PER_TRADING = Decimal(7) / Decimal(5)
_DAYS_PER_YEAR = Decimal(365)

M4_SHORT_VOL = frozenset(
    {
        FamilyId.short_iron_condor_defined,
        FamilyId.short_iron_butterfly_defined,
        FamilyId.long_call_butterfly,
        FamilyId.long_put_butterfly,
    }
)
M4_LONG_VOL = frozenset({FamilyId.long_straddle, FamilyId.long_strangle})


@dataclass(frozen=True, slots=True)
class WeeklyRegime:
    """One week's frozen M4 judgment."""

    computed_at: datetime
    iso_week: tuple[int, int]
    p_up: Decimal
    sigma: Decimal
    implied_vol: Decimal | None
    rv_forecast_annual: Decimal | None
    vol_ratio: Decimal | None
    events: tuple[CalendarEvent, ...]
    events_known: bool
    event_richness: Decimal | None
    view: str
    allowed_families: frozenset[FamilyId]
    features: dict[str, Decimal]
    absent: tuple[str, ...]
    reason_codes: tuple[ReasonCode, ...]
    reference_vix: Decimal | None


def _horizon_days(config: ForecastConfig) -> Decimal:
    return Decimal(config.m4.horizon_minutes) / Decimal(config.session_minutes)


def _change(
    rows: Sequence[GlobalMacroRow], attr: str, *, points: bool = False
) -> Decimal | None:
    values: list[Decimal] = [
        value for row in rows if (value := getattr(row, attr)) is not None
    ]
    if len(values) < 6 or values[-6] == 0:  # noqa: PLR2004 - five-session change
        return None
    if points:
        return (values[-1] - values[-6]) * Decimal(100)
    return values[-1] / values[-6] - _ONE


def _implied_for_horizon(
    inputs: ForecastInputs, horizon_calendar_days: Decimal
) -> Decimal | None:
    today = inputs.as_of.astimezone(inputs.zone).date()
    target = today + timedelta(days=int(horizon_calendar_days))
    future = [(expiry, iv) for expiry, iv in inputs.atm_iv_by_expiry if expiry > today]
    if future:
        _expiry, iv = min(future, key=lambda item: abs((item[0] - target).days))
        scaled = annual_vol(iv)
        if scaled is not None:
            return scaled
    return annual_vol(inputs.current_vix)


def _event_richness(inputs: ForecastInputs, event: CalendarEvent) -> Decimal | None:
    """Implied event move from the two expiries around it, versus its history."""
    if event.typical_move_fraction is None or event.typical_move_fraction <= 0:
        return None
    today = inputs.as_of.astimezone(inputs.zone).date()
    before = [
        row for row in inputs.atm_iv_by_expiry if today < row[0] < event.event_date
    ]
    after = [row for row in inputs.atm_iv_by_expiry if row[0] >= event.event_date]
    if not before or not after:
        return None
    _front_expiry, front_iv = max(before, key=lambda row: row[0])
    back_expiry, back_iv = min(after, key=lambda row: row[0])
    front, back = annual_vol(front_iv), annual_vol(back_iv)
    if front is None or back is None:
        return None
    years = Decimal((back_expiry - today).days) / _DAYS_PER_YEAR
    event_variance = years * (back * back - front * front)
    if event_variance <= 0:
        return _ZERO
    return event_variance.sqrt() / event.typical_move_fraction


def m4_regime(inputs: ForecastInputs, config: ForecastConfig) -> WeeklyRegime:  # noqa: PLR0912, PLR0915
    """Build the weekly direction and volatility view."""
    mode = config.m4
    features: dict[str, Decimal] = {}
    absent: list[str] = []
    reasons: list[ReasonCode] = []
    daily = completed_daily_bars(inputs)
    closes = [bar.close for bar in daily]
    daily_atr = atr(daily, 14)
    for name, period in (("trend_fast", mode.fast_sma), ("trend_slow", mode.slow_sma)):
        average = sma(closes, period)
        if average is not None and daily_atr is not None:
            features[name] = clamp((closes[-1] - average) / daily_atr, -_THREE, _THREE)
        else:
            absent.append(name)
    daily_sigma = stdev(log_returns(closes[-21:]))
    if len(closes) >= 11 and daily_sigma:  # noqa: PLR2004 - ten-session momentum
        features["momentum_10d"] = clamp(
            (closes[-1] / closes[-11]).ln() / (daily_sigma * Decimal(10).sqrt()),
            -_THREE,
            _THREE,
        )
    else:
        absent.append("momentum_10d")
    fii = fii_positioning(inputs)
    if fii is not None:
        features["fii_positioning"] = fii
    else:
        absent.append("fii_positioning")
    cash = [
        row.fii_cash_net
        for row in inputs.reference.flows
        if row.fii_cash_net is not None
    ]
    if cash:
        features["fii_cash_5d"] = clamp(
            sum(cash[-5:], _ZERO) / Decimal(10000), -_THREE, _THREE
        )
    else:
        absent.append("fii_cash_5d")
    rows = inputs.reference.global_macro
    for name, attr, points in (
        ("spx_5d_change", "spx", False),
        ("dxy_5d_change", "dxy", False),
        ("us10y_5d_change_bp", "us10y_yield", True),
        ("crude_5d_change", "crude", False),
        ("usdinr_5d_change", "usdinr", False),
    ):
        value = _change(rows, attr, points=points)
        if value is not None:
            features[name] = value
        else:
            absent.append(name)
    if inputs.macro_bias is not None:
        features["macro_bias"] = inputs.macro_bias

    p_up, missing = score_model(mode, features)
    absent.extend(missing)

    variance = daily_variance_forecast(inputs, har_coefficients(config))
    horizon_days = _horizon_days(config)
    implied = _implied_for_horizon(inputs, horizon_days * _CALENDAR_PER_TRADING)
    rv_annual = None
    if variance is not None:
        rv_annual = (variance * Decimal(config.annual_trading_days)).sqrt()
        sigma = (variance * horizon_days).sqrt()
    elif implied is not None:
        sigma = implied * (horizon_days / Decimal(config.annual_trading_days)).sqrt()
        absent.append("realized_vol_fallback_implied")
    else:
        sigma = Decimal("0.04")
        reasons.append(ReasonCode.FORECAST_INPUT_ABSENT)
    vol_ratio = None if implied is None or not rv_annual else implied / rv_annual
    if vol_ratio is not None:
        features["vol_ratio"] = vol_ratio
    else:
        absent.append("vol_ratio")

    today = inputs.as_of.astimezone(inputs.zone).date()
    window_end = today + timedelta(days=int(horizon_days * _CALENDAR_PER_TRADING))
    events_known = inputs.reference.events_loaded
    events = events_between(
        inputs.reference.events,
        today,
        window_end,
        min_importance=mode.event_min_importance,
    )
    richness = _event_richness(inputs, events[0]) if events else None
    if not events_known:
        absent.append("event_calendar")
    if events:
        features["major_events"] = Decimal(len(events))
        if richness is not None:
            features["event_richness"] = richness
        else:
            absent.append("event_richness")

    view, allowed = _view(
        p_up=p_up,
        vol_ratio=vol_ratio,
        events=events,
        events_known=events_known,
        richness=richness,
        config=config,
    )
    return WeeklyRegime(
        computed_at=inputs.as_of,
        iso_week=_iso_week(today),
        p_up=p_up,
        sigma=sigma,
        implied_vol=implied,
        rv_forecast_annual=rv_annual,
        vol_ratio=vol_ratio,
        events=events,
        events_known=events_known,
        event_richness=richness,
        view=view,
        allowed_families=allowed,
        features=features,
        absent=tuple(dict.fromkeys(absent)),
        reason_codes=tuple(dict.fromkeys(reasons)),
        reference_vix=inputs.current_vix,
    )


def _iso_week(day: date) -> tuple[int, int]:
    calendar = day.isocalendar()
    return calendar.year, calendar.week


def _view(
    *,
    p_up: Decimal,
    vol_ratio: Decimal | None,
    events: Sequence[CalendarEvent],
    events_known: bool,
    richness: Decimal | None,
    config: ForecastConfig,
) -> tuple[str, frozenset[FamilyId]]:
    """Direction view crossed with volatility view."""
    mode = config.m4
    lean = abs(p_up - _HALF)
    direction = direction_from_probability(p_up, mode.directional_margin)
    if direction != 0:
        rich = vol_ratio is not None and vol_ratio >= _ONE
        if direction > 0:
            family = FamilyId.bull_put_credit if rich else FamilyId.bull_call_debit
        else:
            family = FamilyId.bear_call_credit if rich else FamilyId.bear_put_debit
        return "DIRECTIONAL", frozenset({family})
    event_priced = not events or (richness is not None and richness >= _ONE)
    if (
        vol_ratio is not None
        and vol_ratio >= mode.short_vol_min_ratio
        and lean <= mode.range_margin
        and events_known
        and event_priced
    ):
        return "SHORT_VOL", M4_SHORT_VOL
    cheap_event = (
        bool(events) and richness is not None and richness <= mode.event_cheap_ratio
    )
    if (vol_ratio is not None and vol_ratio <= mode.long_vol_max_ratio) or cheap_event:
        return "LONG_VOL", M4_LONG_VOL
    return "NONE", frozenset()


def m4_regime_is_stale(
    regime: WeeklyRegime | None, inputs: ForecastInputs, config: ForecastConfig
) -> bool:
    """Rebuild on a new ISO week, or on a gap or VIX shock since it was built."""
    if regime is None:
        return True
    today = inputs.as_of.astimezone(inputs.zone).date()
    if _iso_week(today) != regime.iso_week:
        return True
    mode = config.m4
    if (
        regime.reference_vix
        and inputs.current_vix is not None
        and abs(inputs.current_vix / regime.reference_vix - _ONE)
        >= mode.shock_vix_change
    ):
        return True
    daily = completed_daily_bars(inputs)
    daily_sigma = stdev(log_returns([bar.close for bar in daily][-21:]))
    if daily and daily_sigma and inputs.bars:
        today_bars = [
            bar
            for bar in inputs.bars
            if bar.start.astimezone(inputs.zone).date() == today
        ]
        if today_bars and regime.computed_at.astimezone(inputs.zone).date() != today:
            gap = abs((today_bars[0].open / daily[-1].close).ln())
            if gap >= mode.shock_gap_sigma * daily_sigma:
                return True
    return False


def m4_view(regime: WeeklyRegime, config: ForecastConfig) -> ModeView:
    """Project the weekly regime into a forecast view."""
    event: ForecastEventKind | None = None
    if regime.view == "SHORT_VOL":
        event = ForecastEventKind.STAY_IN_BAND
    elif regime.view == "LONG_VOL":
        event = ForecastEventKind.LEAVE_BAND
    return assemble_view(
        mode_id=ModeId.M4_STRATEGIC_POSITIONAL,
        config=config,
        mode=config.m4,
        p_up=regime.p_up,
        sigma=regime.sigma,
        implied_vol=regime.implied_vol,
        features=dict(regime.features),
        absent=regime.absent,
        reasons=regime.reason_codes,
        neutral_view=event is not None,
        event_override=event,
        view=regime.view,
        allowed_families=regime.allowed_families,
    )
