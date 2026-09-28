"""Slow-moving reference inputs: pre-market cues, flows, global macro, events.

These rows are produced by the data pipeline. Every forecaster treats a missing
row as absent, never as zero-valued evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

__all__ = [
    "CalendarEvent",
    "GlobalMacroRow",
    "ParticipantFlow",
    "PremarketCues",
    "ReferenceInputs",
    "events_between",
]


@dataclass(frozen=True, slots=True)
class PremarketCues:
    """Overnight/pre-open moves as fractions (0.01 = +1%)."""

    as_of: datetime
    gift_nifty_change: Decimal | None = None
    us_futures_change: Decimal | None = None
    usdinr_change: Decimal | None = None
    crude_change: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ParticipantFlow:
    """End-of-day FII/DII positioning and cash flow (INR crore)."""

    session: date
    fii_index_futures_long: Decimal | None = None
    fii_index_futures_short: Decimal | None = None
    fii_cash_net: Decimal | None = None
    dii_cash_net: Decimal | None = None


@dataclass(frozen=True, slots=True)
class GlobalMacroRow:
    """Daily closes of slow global drivers."""

    session: date
    us10y_yield: Decimal | None = None
    dxy: Decimal | None = None
    crude: Decimal | None = None
    usdinr: Decimal | None = None
    spx: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    """A scheduled market event and its typical absolute NIFTY move."""

    event_date: date
    kind: str
    importance: int
    typical_move_fraction: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ReferenceInputs:
    """Bundle handed to forecasters; ``events_loaded`` separates none from unknown."""

    premarket: PremarketCues | None = None
    flows: tuple[ParticipantFlow, ...] = ()
    global_macro: tuple[GlobalMacroRow, ...] = ()
    events: tuple[CalendarEvent, ...] = ()
    events_loaded: bool = False


def events_between(
    events: Sequence[CalendarEvent], start: date, end: date, *, min_importance: int
) -> tuple[CalendarEvent, ...]:
    """Events inside ``[start, end]`` at or above an importance level."""
    return tuple(
        item
        for item in events
        if start <= item.event_date <= end and item.importance >= min_importance
    )
