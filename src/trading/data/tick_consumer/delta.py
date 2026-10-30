"""Session and rolling delta from signed ticks."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from trading.data.tick_consumer.models import DeltaState, SignedTick, TickSign

_IST = ZoneInfo("Asia/Kolkata")

__all__ = ["DeltaTracker", "apply_signed_tick"]


def apply_signed_tick(state: DeltaState, tick: SignedTick, *, now: datetime) -> None:
    """Update session totals and five-minute rolling delta."""
    size = tick.trade_size or tick.volume or 0
    if tick.sign is TickSign.BUY:
        state.session_buy_volume += size
    elif tick.sign is TickSign.SELL:
        state.session_sell_volume += size
    state.session_delta = state.session_buy_volume - state.session_sell_volume
    state.cumulative_delta = state.session_delta
    local = now.astimezone(_IST)
    cutoff = local - timedelta(minutes=5)
    if tick.received_at.astimezone(_IST) >= cutoff:
        if tick.sign is TickSign.BUY:
            state.delta_5m += size
        elif tick.sign is TickSign.SELL:
            state.delta_5m -= size


class DeltaTracker:
    """Maintains delta state for one session."""

    def __init__(self, session_date: date) -> None:
        self._state = DeltaState(session_date=session_date)
        self._recent: list[tuple[datetime, SignedTick]] = []

    @property
    def state(self) -> DeltaState:
        return self._state

    def on_tick(self, tick: SignedTick, *, now: datetime) -> None:
        """Record tick and refresh rolling five-minute delta."""
        self._recent.append((now, tick))
        apply_signed_tick(self._state, tick, now=now)
        cutoff = now - timedelta(minutes=5)
        self._recent = [(at, item) for at, item in self._recent if at >= cutoff]
        buy = sell = 0
        for _, item in self._recent:
            size = item.trade_size or item.volume or 0
            if item.sign is TickSign.BUY:
                buy += size
            elif item.sign is TickSign.SELL:
                sell += size
        self._state.delta_5m = buy - sell
