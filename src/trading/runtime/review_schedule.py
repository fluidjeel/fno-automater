"""Due-slot detection for twice-daily PAPER positional reviews.

NSE 10:30 and 14:30 IST are the required path. MCX slots are config-ready and
stay idle until populated. Missed slots replay once if the process returns
while still before EOD and the slot is not yet recorded. When multiple slots
were missed, one current recovery assessment runs and records every missed id.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING

from trading.domain.enums import Exchange, ReviewSlotId

if TYPE_CHECKING:
    from trading.identification.calendar import TradingCalendarPort

__all__ = [
    "DueReviewWork",
    "ReviewSlot",
    "due_review_slots",
    "next_review_slot_id",
    "parse_hhmm",
]


@dataclass(frozen=True, slots=True)
class ReviewSlot:
    """One configured review clock time in exchange-local hours."""

    slot_id: ReviewSlotId
    local: time

    @property
    def venue(self) -> Exchange:
        return self.slot_id.venue


@dataclass(frozen=True, slots=True)
class DueReviewWork:
    """One review execution unit: a single slot or a collapsed recovery bundle."""

    slot: ReviewSlot
    missed_slot_ids: tuple[ReviewSlotId, ...] = ()

    @property
    def slot_id(self) -> ReviewSlotId:
        return self.slot.slot_id

    @property
    def is_recovery(self) -> bool:
        return len(self.missed_slot_ids) > 1


def parse_hhmm(value: str) -> time:
    """Parse a config ``HH:MM`` clock time."""
    hour, minute = (int(part) for part in value.split(":", 1))
    return time(hour, minute)


def next_review_slot_id(
    current: ReviewSlotId,
    slots: Iterable[ReviewSlot],
) -> ReviewSlotId | None:
    """Return the next configured slot after ``current``, if any."""
    ordered = sorted(slots, key=lambda item: item.local)
    slot_ids = [item.slot_id for item in ordered]
    try:
        index = slot_ids.index(current)
    except ValueError:
        return None
    if index + 1 >= len(slot_ids):
        return None
    return slot_ids[index + 1]


def due_review_slots(
    *,
    now_local: datetime,
    session_open: time,
    eod: time,
    slots: Iterable[ReviewSlot],
    recorded: frozenset[tuple[date, ReviewSlotId]],
    calendar: TradingCalendarPort | None = None,
) -> tuple[DueReviewWork, ...]:
    """Slots whose local time has arrived today and are not yet recorded.

    A slot is due when the process is inside the session (open inclusive, EOD
    exclusive), ``now >= slot.local``, and ``(session_date, slot_id)`` has not
    been persisted. Restart after a missed 10:30 therefore fires that slot once
    if the session is still open. When more than one slot was missed, return one
    recovery work item that records every missed id.
    """
    local_time = now_local.time()
    if local_time < session_open or local_time >= eod:
        return ()
    session_date = now_local.date()
    pending: list[ReviewSlot] = []
    for slot in sorted(slots, key=lambda item: item.local):
        if local_time < slot.local:
            continue
        if (session_date, slot.slot_id) in recorded:
            continue
        if calendar is not None and calendar.should_skip_positional_review(
            now_local, slot.local
        ):
            continue
        pending.append(slot)
    if not pending:
        return ()
    if len(pending) == 1:
        return (DueReviewWork(slot=pending[0]),)
    return (
        DueReviewWork(
            slot=pending[-1],
            missed_slot_ids=tuple(item.slot_id for item in pending),
        ),
    )
