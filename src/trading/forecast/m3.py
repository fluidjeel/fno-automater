"""M3 tactical forecaster: swing direction plus IV-driven debit or credit choice."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal

from trading.config.forecast import ForecastConfig
from trading.domain.enums import FamilyId, ModeId, ReasonCode
from trading.forecast.bars import Bar, atr, resample_intraday, sma
from trading.forecast.common import (
    assemble_view,
    completed_daily_bars,
    daily_variance_forecast,
    har_coefficients,
    horizon_sigma,
    score_model,
)
from trading.forecast.inputs import ForecastInputs, ModeView, annual_vol, clamp
from trading.forecast.volatility import log_returns, stdev

__all__ = ["fii_positioning", "m3_view", "swing_pivots"]

_ZERO = Decimal(0)
_ONE = Decimal(1)
_HALF = Decimal("0.5")
_THREE = Decimal(3)


def swing_pivots(bars: Sequence[Bar]) -> tuple[list[Bar], list[Bar]]:
    """Three-bar pivot highs and lows, oldest first."""
    highs: list[Bar] = []
    lows: list[Bar] = []
    for index in range(1, len(bars) - 1):
        bar, before, after = bars[index], bars[index - 1], bars[index + 1]
        if bar.high > before.high and bar.high > after.high:
            highs.append(bar)
        if bar.low < before.low and bar.low < after.low:
            lows.append(bar)
    return highs, lows


def _swing_structure(highs: Sequence[Bar], lows: Sequence[Bar]) -> Decimal | None:
    if len(highs) < 2 or len(lows) < 2:  # noqa: PLR2004 - two swings define structure
        return None
    rising = highs[-1].high > highs[-2].high and lows[-1].low > lows[-2].low
    falling = highs[-1].high < highs[-2].high and lows[-1].low < lows[-2].low
    return _ONE if rising else -_ONE if falling else _ZERO


def fii_positioning(inputs: ForecastInputs) -> Decimal | None:
    """Latest FII index-futures long share minus one half."""
    for row in reversed(inputs.reference.flows):
        long_, short = row.fii_index_futures_long, row.fii_index_futures_short
        if long_ is not None and short is not None and long_ + short > 0:
            return long_ / (long_ + short) - _HALF
    return None


def _m3_features(
    inputs: ForecastInputs, config: ForecastConfig
) -> tuple[dict[str, Decimal], list[str], list[Bar], list[Bar], Decimal | None]:
    """Swing features plus the pivots and daily ATR invalidation needs."""
    mode = config.m3
    features: dict[str, Decimal] = {}
    absent: list[str] = []
    daily = completed_daily_bars(inputs)
    daily_atr = atr(daily, 14)
    closes = [bar.close for bar in daily]
    average = sma(closes, mode.daily_trend_sma)
    if average is not None and daily_atr is not None:
        features["daily_trend"] = clamp(
            (closes[-1] - average) / daily_atr, -_THREE, _THREE
        )
    else:
        absent.append("daily_trend")
    hourly = resample_intraday(inputs.bars, minutes=60, zone=inputs.zone)
    recent = hourly[-mode.swing_lookback_bars :]
    highs, lows = swing_pivots(recent)
    structure = _swing_structure(highs, lows)
    if structure is not None:
        features["swing_structure"] = structure
    else:
        absent.append("swing_structure")
    daily_sigma = stdev(log_returns(closes[-21:]))
    if len(closes) >= 6 and daily_sigma:  # noqa: PLR2004 - five-session momentum
        features["momentum_5d"] = clamp(
            (closes[-1] / closes[-6]).ln() / (daily_sigma * Decimal(5).sqrt()),
            -_THREE,
            _THREE,
        )
    else:
        absent.append("momentum_5d")
    window = daily[-20:]
    if window:
        high = max(bar.high for bar in window)
        low = min(bar.low for bar in window)
        if high > low:
            features["range_position"] = (inputs.spot - low) / (high - low) - _HALF
    fii = fii_positioning(inputs)
    if fii is not None:
        features["fii_positioning"] = fii
    else:
        absent.append("fii_positioning")
    if inputs.macro_bias is not None:
        features["macro_bias"] = inputs.macro_bias
    return features, absent, highs, lows, daily_atr


def m3_view(inputs: ForecastInputs, config: ForecastConfig) -> ModeView:
    """Days-to-two-weeks swing view; evaluated on 60m and daily bars."""
    mode = config.m3
    reasons: list[ReasonCode] = []
    features, absent, highs, lows, daily_atr = _m3_features(inputs, config)
    p_up, missing = score_model(mode, features)
    absent.extend(missing)
    implied = annual_vol(inputs.current_vix)
    sigma, vol_absent = horizon_sigma(
        config,
        mode,
        bars=inputs.bars,
        daily_variance=daily_variance_forecast(inputs, har_coefficients(config)),
        implied_vol=implied,
        intraday_weight=_ZERO,
    )
    absent.extend(item for item in vol_absent if item != "intraday_realized_vol")
    if sigma is None:
        sigma = Decimal("0.02")
        reasons.append(ReasonCode.FORECAST_INPUT_ABSENT)

    iv_pct = inputs.iv_percentile
    if iv_pct is None:
        absent.append("iv_percentile")
    buffer = _ZERO if daily_atr is None else mode.invalidation_buffer_atr * daily_atr
    below = None if not lows else lows[-1].low - buffer
    above = None if not highs else highs[-1].high + buffer
    view = assemble_view(
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
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
    structure_view, allowed = _structure_choice(view.direction, iv_pct, config)
    return replace(view, view=structure_view, allowed_families=allowed)


def _structure_choice(
    direction: int, iv_percentile: Decimal | None, config: ForecastConfig
) -> tuple[str, frozenset[FamilyId]]:
    """Buy premium when IV is cheap, sell it when rich, either in between."""
    if direction == 0:
        return "NONE", frozenset()
    debit = FamilyId.bull_call_debit if direction > 0 else FamilyId.bear_put_debit
    credit = FamilyId.bull_put_credit if direction > 0 else FamilyId.bear_call_credit
    mode = config.m3
    if iv_percentile is not None and iv_percentile <= mode.debit_max_iv_percentile:
        return "DEBIT", frozenset({debit})
    if iv_percentile is not None and iv_percentile >= mode.credit_min_iv_percentile:
        return "CREDIT", frozenset({credit})
    return "EITHER", frozenset({debit, credit})
