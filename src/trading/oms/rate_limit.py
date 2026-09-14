"""Bounded order submission rate limiting."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from trading.domain.clock import Clock
from trading.domain.enums import ReasonCode

__all__ = ["OrderRateLimiter", "RateLimitExceededError"]


class RateLimitExceededError(Exception):
    """Raised when a submit would exceed configured broker or exchange limits."""

    reason_code = ReasonCode.RATE_LIMIT

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass
class OrderRateLimiter:
    """Sliding-window submit throttle. None limits disable enforcement."""

    clock: Clock
    max_per_second: int | None = None
    max_per_day: int | None = None
    _second_window: deque[datetime] = field(default_factory=deque, init=False)
    _day_window: deque[datetime] = field(default_factory=deque, init=False)

    def check_and_consume(self) -> None:
        """Reject when the next submit would breach configured limits."""
        now = self.clock.now_utc()
        if self.max_per_second is not None:
            self._prune(self._second_window, now, timedelta(seconds=1))
            if len(self._second_window) >= self.max_per_second:
                raise RateLimitExceededError(
                    f"order rate exceeds {self.max_per_second} per second"
                )
            self._second_window.append(now)
        if self.max_per_day is not None:
            self._prune(self._day_window, now, timedelta(days=1))
            if len(self._day_window) >= self.max_per_day:
                raise RateLimitExceededError(
                    f"order rate exceeds {self.max_per_day} per day"
                )
            self._day_window.append(now)

    @staticmethod
    def _prune(
        window: deque[datetime],
        now: datetime,
        horizon: timedelta,
    ) -> None:
        cutoff = now - horizon
        while window and window[0] < cutoff:
            window.popleft()
