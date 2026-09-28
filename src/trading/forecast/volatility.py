"""Realized-volatility measurement and HAR-style forecasting."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading.forecast.bars import Bar, session_bars

__all__ = [
    "HarCoefficients",
    "daily_realized_variance",
    "har_variance_forecast",
    "intraday_bar_sigma",
    "log_returns",
    "stdev",
]

_ZERO = Decimal(0)
_MIN_SAMPLES = 2


def log_returns(closes: Sequence[Decimal]) -> tuple[Decimal, ...]:
    """Consecutive log returns of a positive price series."""
    return tuple(
        (closes[i] / closes[i - 1]).ln()
        for i in range(1, len(closes))
        if closes[i] > 0 and closes[i - 1] > 0
    )


def stdev(values: Sequence[Decimal]) -> Decimal | None:
    """Sample standard deviation, or None with fewer than two samples."""
    if len(values) < _MIN_SAMPLES:
        return None
    mean = sum(values, _ZERO) / Decimal(len(values))
    variance = sum(((item - mean) ** 2 for item in values), _ZERO) / Decimal(
        len(values) - 1
    )
    return variance.sqrt()


def intraday_bar_sigma(bars: Sequence[Bar], window: int) -> Decimal | None:
    """Per-bar log-return stdev over the last ``window`` bars within sessions."""
    returns: list[Decimal] = []
    previous: Bar | None = None
    for bar in bars[-(window + 1) :]:
        if previous is not None and bar.start.date() == previous.start.date():
            returns.append((bar.close / previous.close).ln())
        previous = bar
    return stdev(returns)


def daily_realized_variance(
    bars: Sequence[Bar], zone: ZoneInfo, *, include_overnight: bool = True
) -> tuple[tuple[date, Decimal], ...]:
    """Per-session realized variance: sum of squared intraday log returns.

    The overnight gap carries information a weeks-horizon view must price, so
    it is included by default.
    """
    rows: list[tuple[date, Decimal]] = []
    previous_close: Decimal | None = None
    for day, group in session_bars(bars, zone):
        closes = [bar.close for bar in group]
        variance = sum((item * item for item in log_returns(closes)), _ZERO)
        if include_overnight and previous_close is not None and group[0].open > 0:
            gap = (group[0].open / previous_close).ln()
            variance += gap * gap
        rows.append((day, variance))
        previous_close = group[-1].close
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class HarCoefficients:
    """Corsi HAR weights on daily, weekly (5) and monthly (22) mean variance."""

    intercept: Decimal
    daily: Decimal
    weekly: Decimal
    monthly: Decimal


def har_variance_forecast(
    daily_variance: Sequence[Decimal],
    coefficients: HarCoefficients,
    *,
    min_days: int = 5,
) -> Decimal | None:
    """Next-day variance forecast; the monthly term uses what history exists."""
    if len(daily_variance) < min_days:
        return None
    last = daily_variance[-1]
    weekly = sum(daily_variance[-5:], _ZERO) / Decimal(len(daily_variance[-5:]))
    monthly_window = daily_variance[-22:]
    monthly = sum(monthly_window, _ZERO) / Decimal(len(monthly_window))
    forecast = (
        coefficients.intercept
        + coefficients.daily * last
        + coefficients.weekly * weekly
        + coefficients.monthly * monthly
    )
    return forecast if forecast > 0 else monthly if monthly > 0 else None
