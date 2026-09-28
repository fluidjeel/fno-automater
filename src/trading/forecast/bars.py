"""Typed OHLCV bars, resampling and simple bar statistics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

__all__ = [
    "Bar",
    "atr",
    "daily_bars",
    "parse_bars",
    "resample_intraday",
    "session_bars",
    "sma",
]

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class Bar:
    """One completed OHLCV bar; ``start`` is aware UTC."""

    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def parse_bars(
    rows: Sequence[object],
    *,
    bar_seconds: int,
    as_of: datetime,
) -> tuple[Bar, ...]:
    """Parse completed bars from BAR_SNAPSHOT rows; malformed rows are skipped."""
    parsed: list[Bar] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        try:
            start = datetime.fromtimestamp(int(row["timestamp"]), tz=UTC)
            bar = Bar(
                start=start,
                open=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=Decimal(str(row.get("volume", 0))),
            )
        except (KeyError, ArithmeticError, TypeError, ValueError, OSError):
            continue
        if start + timedelta(seconds=bar_seconds) > as_of:
            continue
        if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.volume < 0:
            continue
        parsed.append(bar)
    parsed.sort(key=lambda item: item.start)
    return tuple(parsed)


def session_bars(
    bars: Sequence[Bar], zone: ZoneInfo
) -> tuple[tuple[date, tuple[Bar, ...]], ...]:
    """Group bars by exchange-local session date, ascending."""
    groups: dict[date, list[Bar]] = {}
    for bar in bars:
        groups.setdefault(bar.start.astimezone(zone).date(), []).append(bar)
    return tuple((day, tuple(groups[day])) for day in sorted(groups))


def _merge(group: Sequence[Bar]) -> Bar:
    return Bar(
        start=group[0].start,
        open=group[0].open,
        high=max(item.high for item in group),
        low=min(item.low for item in group),
        close=group[-1].close,
        volume=sum((item.volume for item in group), _ZERO),
    )


def daily_bars(bars: Sequence[Bar], zone: ZoneInfo) -> tuple[Bar, ...]:
    """One bar per session built from intraday bars."""
    return tuple(_merge(group) for _day, group in session_bars(bars, zone))


def resample_intraday(
    bars: Sequence[Bar],
    *,
    minutes: int,
    zone: ZoneInfo,
    session_open: tuple[int, int] = (9, 15),
) -> tuple[Bar, ...]:
    """Resample within each session; buckets are anchored at the session open."""
    if minutes <= 0:
        raise ValueError("resample minutes must be positive")
    out: list[Bar] = []
    for _day, group in session_bars(bars, zone):
        buckets: dict[int, list[Bar]] = {}
        for bar in group:
            local = bar.start.astimezone(zone)
            offset = (
                (local.hour - session_open[0]) * 60 + local.minute - session_open[1]
            )
            buckets.setdefault(max(offset, 0) // minutes, []).append(bar)
        out.extend(_merge(buckets[key]) for key in sorted(buckets))
    return tuple(out)


def atr(bars: Sequence[Bar], period: int) -> Decimal | None:
    """Simple-average true range over the last ``period`` bars."""
    if period <= 0 or len(bars) < 2:  # noqa: PLR2004 - a true range needs a prior close
        return None
    ranges: list[Decimal] = []
    for index in range(1, len(bars)):
        bar, previous = bars[index], bars[index - 1].close
        ranges.append(
            max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous))
        )
    window = ranges[-period:]
    value = sum(window, _ZERO) / Decimal(len(window))
    return value if value > 0 else None


def sma(values: Sequence[Decimal], period: int) -> Decimal | None:
    """Simple moving average of the last ``period`` values."""
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:], _ZERO) / Decimal(period)
