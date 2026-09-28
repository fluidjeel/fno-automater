"""Shared Fyers REST throttle (10/s, 200/min per account)."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trading.domain.clock import Clock

__all__ = ["FyersRestRateLimiter"]

_FYERS_MAX_PER_SECOND = 10
_FYERS_MAX_PER_MINUTE = 200
_DEFAULT_MAX_WAIT = timedelta(seconds=15)
_POLL_INTERVAL = 0.05


@dataclass
class FyersRestRateLimiter:
    """Sliding-window REST call budget shared across paper session feeds."""

    clock: Clock
    max_per_second: int = _FYERS_MAX_PER_SECOND
    max_per_minute: int = _FYERS_MAX_PER_MINUTE
    _second_window: deque[datetime] = field(default_factory=deque, init=False)
    _minute_window: deque[datetime] = field(default_factory=deque, init=False)

    def acquire(self) -> bool:
        """Reserve one REST call when under limits; return False when capped."""
        now = self.clock.now_utc()
        self._prune(self._second_window, now, timedelta(seconds=1))
        self._prune(self._minute_window, now, timedelta(minutes=1))
        if len(self._second_window) >= self.max_per_second:
            return False
        if len(self._minute_window) >= self.max_per_minute:
            return False
        self._second_window.append(now)
        self._minute_window.append(now)
        return True

    def acquire_or_wait(
        self,
        *,
        max_wait: timedelta = _DEFAULT_MAX_WAIT,
        sleep: Callable[[float], None] | None = None,
    ) -> bool:
        """Block until a token is available or ``max_wait`` elapses."""
        sleeper = sleep if sleep is not None else time.sleep
        deadline = self.clock.now_utc() + max_wait
        while True:
            if self.acquire():
                return True
            now = self.clock.now_utc()
            if now >= deadline:
                return False
            remaining = (deadline - now).total_seconds()
            sleeper(min(_POLL_INTERVAL, remaining))

    @staticmethod
    def _prune(
        window: deque[datetime],
        now: datetime,
        horizon: timedelta,
    ) -> None:
        cutoff = now - horizon
        while window and window[0] < cutoff:
            window.popleft()
