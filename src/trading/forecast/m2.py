"""M2 directional forecaster: move detection over hours to a few sessions."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading.config.forecast import ForecastConfig
from trading.domain.enums import ModeId, ReasonCode
from trading.forecast.bars import Bar, atr, session_bars, sma
from trading.forecast.common import (
    assemble_view,
    completed_daily_bars,
    daily_variance_forecast,
    har_coefficients,
    horizon_sigma,
    score_model,
)
from trading.forecast.inputs import ForecastInputs, ModeView, annual_vol, clamp, sign

__all__ = ["M2Levels", "m2_levels", "m2_view"]

_ZERO = Decimal(0)
_ONE = Decimal(1)
_THREE = Decimal(3)
_INTRADAY_WEIGHT = Decimal("0.5")


@dataclass(frozen=True, slots=True)
class M2Levels:
    """Intraday reference levels for breakout and invalidation."""

    opening_high: Decimal
    opening_low: Decimal
    prior_high: Decimal | None
    prior_low: Decimal | None
    prior_close: Decimal | None
    today_open: Decimal
    session_high: Decimal
    session_low: Decimal
    vwap: Decimal
    atr_intraday: Decimal | None
    last_bar: Bar


def m2_levels(
    inputs: ForecastInputs, opening_minutes: int, bar_seconds: int
) -> M2Levels | None:
    """Opening range, prior-session levels and VWAP for the current session."""
    sessions = session_bars(inputs.bars, inputs.zone)
    today = inputs.as_of.astimezone(inputs.zone).date()
    if not sessions or sessions[-1][0] != today:
        return None
    current = sessions[-1][1]
    opening_count = max(1, (opening_minutes * 60) // bar_seconds)
    if len(current) < opening_count:
        return None
    opening = current[:opening_count]
    prior = sessions[-2][1] if len(sessions) >= 2 else ()  # noqa: PLR2004
    volume = sum((bar.volume for bar in current), _ZERO)
    vwap = (
        current[-1].close
        if volume <= 0
        else sum(
            ((bar.high + bar.low + bar.close) / _THREE * bar.volume for bar in current),
            _ZERO,
        )
        / volume
    )
    return M2Levels(
        opening_high=max(bar.high for bar in opening),
        opening_low=min(bar.low for bar in opening),
        prior_high=max((bar.high for bar in prior), default=None),
        prior_low=min((bar.low for bar in prior), default=None),
        prior_close=prior[-1].close if prior else None,
        today_open=current[0].open,
        session_high=max(bar.high for bar in current),
        session_low=min(bar.low for bar in current),
        vwap=vwap,
        atr_intraday=atr(inputs.bars, 14),
        last_bar=current[-1],
    )


def m2_view(inputs: ForecastInputs, config: ForecastConfig) -> ModeView:  # noqa: PLR0912, PLR0915
    """Breakout, level, positioning and pre-market evidence into P(up)."""
    mode = config.m2
    features: dict[str, Decimal] = {}
    absent: list[str] = []
    reasons: list[ReasonCode] = []
    levels = m2_levels(inputs, mode.opening_range_minutes, config.bar_seconds)
    completed_daily = completed_daily_bars(inputs)
    daily_atr = atr(completed_daily, 14)
    spot = inputs.spot

    if levels is None:
        absent.append("session_levels")
    else:
        last = levels.last_bar
        confirm = (
            levels.atr_intraday is not None
            and last.high - last.low
            >= mode.breakout_min_range_atr * levels.atr_intraday
        )
        orb = _ZERO
        if last.close > levels.opening_high:
            orb = _ONE
        elif last.close < levels.opening_low:
            orb = -_ONE
        features["orb_break"] = orb
        features["orb_break_confirmed"] = orb if confirm else _ZERO
        if levels.prior_high is not None and levels.prior_low is not None:
            prior_break = _ZERO
            if last.close > levels.prior_high:
                prior_break = _ONE
            elif last.close < levels.prior_low:
                prior_break = -_ONE
            features["prior_day_break"] = prior_break
        else:
            absent.append("prior_day_break")
        if levels.prior_close is not None and daily_atr is not None:
            features["gap_z"] = clamp(
                (levels.today_open - levels.prior_close) / daily_atr, -_THREE, _THREE
            )
        else:
            absent.append("gap_z")
        if levels.atr_intraday is not None:
            features["vwap_distance"] = clamp(
                (spot - levels.vwap) / levels.atr_intraday, -_THREE, _THREE
            )
        if daily_atr is not None:
            expansion = (levels.session_high - levels.session_low) / daily_atr
            features["signed_range_expansion"] = clamp(
                expansion * sign(spot - levels.today_open), -_THREE, _THREE
            )

    closes = [bar.close for bar in completed_daily]
    average = sma(closes, mode.daily_trend_sma)
    if average is not None and daily_atr is not None:
        features["daily_trend"] = clamp(
            (closes[-1] - average) / daily_atr, -_THREE, _THREE
        )
    else:
        absent.append("daily_trend")

    market = inputs.market
    if market is not None and market.normalized_return_60m is not None:
        features["normalized_60m"] = market.normalized_return_60m
    alignment_parts = [
        sign(features.get("daily_trend")),
        sign(None if market is None else market.return_60m),
        sign(None if market is None else market.return_15m),
    ]
    features["mtf_alignment"] = sum(alignment_parts, _ZERO) / _THREE

    positioning = inputs.positioning
    if positioning is not None and positioning.pcr_change is not None:
        features["pcr_change"] = clamp(positioning.pcr_change, -_ONE, _ONE)
    else:
        absent.append("pcr_change")
    if positioning is not None and positioning.oi_wall_shift is not None:
        features["oi_wall_shift"] = clamp(positioning.oi_wall_shift, -_THREE, _THREE)
    if inputs.current_vix is not None and inputs.previous_vix:
        features["vix_change"] = clamp(
            inputs.current_vix / inputs.previous_vix - _ONE,
            Decimal("-0.5"),
            Decimal("0.5"),
        )
    else:
        absent.append("vix_change")
    cues = inputs.reference.premarket
    today = inputs.as_of.astimezone(inputs.zone).date()
    if cues is not None and cues.as_of.astimezone(inputs.zone).date() == today:
        parts = [
            value
            for value in (cues.gift_nifty_change, cues.us_futures_change)
            if value is not None
        ]
        if parts:
            features["premarket_bias"] = clamp(
                sum(parts, _ZERO) / Decimal(len(parts)),
                Decimal("-0.05"),
                Decimal("0.05"),
            )
    if "premarket_bias" not in features:
        absent.append("premarket_bias")
    if inputs.macro_bias is not None:
        features["macro_bias"] = inputs.macro_bias

    p_up, missing = score_model(mode, features)
    absent.extend(missing)
    implied = annual_vol(inputs.current_vix)
    sigma, vol_absent = horizon_sigma(
        config,
        mode,
        bars=inputs.bars,
        daily_variance=daily_variance_forecast(inputs, har_coefficients(config)),
        implied_vol=implied,
        intraday_weight=_INTRADAY_WEIGHT,
    )
    absent.extend(vol_absent)
    if sigma is None:
        sigma = Decimal("0.005")
        reasons.append(ReasonCode.FORECAST_INPUT_ABSENT)
    below, above = _invalidation(features, levels, mode.invalidation_buffer_atr)
    return assemble_view(
        mode_id=ModeId.M2_DIRECTIONAL,
        config=config,
        mode=mode,
        p_up=p_up,
        sigma=sigma,
        implied_vol=implied,
        features=features,
        absent=absent,
        reasons=reasons,
        invalidation_below=below,
        invalidation_above=above,
    )


def _invalidation(
    features: dict[str, Decimal], levels: M2Levels | None, buffer_atr: Decimal
) -> tuple[Decimal | None, Decimal | None]:
    """Back inside the broken level (or across VWAP) invalidates the move."""
    if levels is None or levels.atr_intraday is None:
        return None, None
    buffer = buffer_atr * levels.atr_intraday
    orb = features.get("orb_break", _ZERO)
    prior = features.get("prior_day_break", _ZERO)
    below_level = levels.vwap
    if prior > 0 and levels.prior_high is not None:
        below_level = levels.prior_high
    elif orb > 0:
        below_level = levels.opening_high
    above_level = levels.vwap
    if prior < 0 and levels.prior_low is not None:
        above_level = levels.prior_low
    elif orb < 0:
        above_level = levels.opening_low
    return below_level - buffer, above_level + buffer
