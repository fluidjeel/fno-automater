"""Time as an injected port. Nothing in the system reads the ambient wall clock.

Invariant 21: the same snapshot, config and code version must produce the same
decision. That is impossible if any component calls datetime.now() directly, so
time arrives through a Clock and every timestamp is timezone-aware.

Invariant 10 and 20: clock drift blocks time-sensitive entries, and replay must
not leak future data. Both need a clock that tests can control exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Protocol, runtime_checkable

__all__ = [
    "Clock",
    "FrozenClock",
    "SteppingClock",
    "TimeError",
    "ensure_utc",
    "to_exchange_local",
]


class TimeError(ValueError):
    """Raised for naive, non-UTC or non-monotonic timestamps."""


def ensure_utc(value: datetime, field_name: str = "timestamp") -> datetime:
    """Reject naive datetimes and normalize to UTC.

    A naive datetime is the single most common source of silent off-by-hours
    errors in a system that spans exchange-local sessions and UTC storage, so it
    is rejected rather than assumed.
    """
    if not isinstance(value, datetime):
        raise TimeError(f"{field_name} must be a datetime, got {type(value).__name__}")
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise TimeError(
            f"{field_name} is naive; supply an aware datetime so the exchange "
            "session it belongs to is unambiguous"
        )
    return value.astimezone(UTC)


def to_exchange_local(value: datetime, exchange_tz: tzinfo) -> datetime:
    """Render a UTC instant in exchange-local time for session logic.

    The exchange timezone is configuration, never a constant in this module.
    """
    return ensure_utc(value).astimezone(exchange_tz)


@runtime_checkable
class Clock(Protocol):
    """Source of the current instant. The only sanctioned way to read time."""

    def now_utc(self) -> datetime:
        """Current instant as an aware UTC datetime."""
        ...

    def now_in(self, exchange_tz: tzinfo) -> datetime:
        """Current instant rendered in an exchange's local timezone."""
        ...


@dataclass(slots=True)
class FrozenClock:
    """A clock stopped at one instant. Use where elapsed time must not matter."""

    instant: datetime

    def __post_init__(self) -> None:
        self.instant = ensure_utc(self.instant, "FrozenClock.instant")

    def now_utc(self) -> datetime:
        return self.instant

    def now_in(self, exchange_tz: tzinfo) -> datetime:
        return to_exchange_local(self.instant, exchange_tz)

    def set(self, instant: datetime) -> None:
        """Jump to an instant. Moving backwards is allowed only here, for tests."""
        self.instant = ensure_utc(instant, "instant")

    def advance(self, delta: timedelta) -> None:
        if delta < timedelta(0):
            raise TimeError("FrozenClock.advance requires a non-negative delta")
        self.instant += delta


@dataclass(slots=True)
class SteppingClock:
    """A clock that advances by a fixed step on every read.

    Drives scenario tests where each observation must land on a distinct,
    predictable instant, for example staleness and timeout boundaries.
    """

    instant: datetime
    step: timedelta = field(default_factory=lambda: timedelta(milliseconds=1))

    def __post_init__(self) -> None:
        self.instant = ensure_utc(self.instant, "SteppingClock.instant")
        if self.step <= timedelta(0):
            raise TimeError("SteppingClock.step must be positive to stay monotonic")

    def now_utc(self) -> datetime:
        current = self.instant
        self.instant = current + self.step
        return current

    def now_in(self, exchange_tz: tzinfo) -> datetime:
        return to_exchange_local(self.now_utc(), exchange_tz)
