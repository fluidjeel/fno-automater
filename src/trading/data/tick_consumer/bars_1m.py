"""Aggregate signed ticks into IST one-minute bars."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading.data.tick_consumer.models import Bar1m, SignedTick, TickSign

_IST = ZoneInfo("Asia/Kolkata")

__all__ = ["BarAggregator", "minute_start_ist"]


def minute_start_ist(instant: datetime) -> datetime:
    """Floor to minute boundary in IST."""
    local = instant.astimezone(_IST).replace(second=0, microsecond=0)
    return local


class BarAggregator:
    """Builds one open bar per symbol."""

    def __init__(self, symbol: str) -> None:
        self._symbol = symbol
        self._bar_start: datetime | None = None
        self._open = self._high = self._low = self._close = Decimal("0")
        self._volume = 0
        self._buy = self._sell = self._unknown = 0
        self._tick_count = 0

    def ingest(self, tick: SignedTick) -> Bar1m | None:
        """Add tick; return completed bar when minute rolls."""
        start = minute_start_ist(tick.received_at)
        completed: Bar1m | None = None
        if self._bar_start is not None and start > self._bar_start:
            completed = self._flush()
        if self._bar_start is None or start > self._bar_start:
            self._bar_start = start
            price = Decimal(tick.ltp)
            self._open = self._high = self._low = self._close = price
            self._volume = 0
            self._buy = self._sell = self._unknown = 0
            self._tick_count = 0
        price = Decimal(tick.ltp)
        self._close = price
        if price > self._high:
            self._high = price
        if price < self._low:
            self._low = price
        size = tick.trade_size or tick.volume or 0
        self._volume += size
        if tick.sign is TickSign.BUY:
            self._buy += size
        elif tick.sign is TickSign.SELL:
            self._sell += size
        else:
            self._unknown += size
        self._tick_count += 1
        return completed

    def _flush(self) -> Bar1m:
        assert self._bar_start is not None
        bar = Bar1m(
            symbol=self._symbol,
            bar_start_ist=self._bar_start,
            open=str(self._open),
            high=str(self._high),
            low=str(self._low),
            close=str(self._close),
            volume=self._volume,
            buy_volume=self._buy,
            sell_volume=self._sell,
            unknown_volume=self._unknown,
            tick_count=self._tick_count,
        )
        self._bar_start = None
        return bar
