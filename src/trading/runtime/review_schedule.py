"""Due-slot detection for twice-daily PAPER positional reviews.

NSE 10:30 and 14:30 IST are the required path. MCX slots are config-ready and
stay idle until populated. Missed slots replay once if the process returns
while still before EOD and the slot is not yet recorded.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time

from trading.domain.enums import Exchange, ReviewSlotId

__all__ = ["ReviewSlot", "due_review_slots", "parse_hhmm"]


@dataclass(frozen=True, slots=True)
class ReviewSlot:
    """One configured review clock time in exchange-local hours."""

    slot_id: ReviewSlotId
    local: time

    @property
    def venue(self) -> Exchange:
        return self.slot_id.venue


def parse_hhmm(value: str) -> time:
    """Parse a config ``HH:MM`` clock time."""
    hour, minute = (int(part) for part in value.split(":", 1))
    return time(hour, minute)


def due_review_slots(
    *,
    now_local: datetime,
    session_open: time,
    eod: time,
    slots: Iterable[ReviewSlot],
    recorded: frozenset[tuple[date, ReviewSlotId]],
) -> tuple[ReviewSlot, ...]:
    """Slots whose local time has arrived today and are not yet recorded.

    A slot is due when the process is inside the session (open inclusive, EOD
    exclusive), ``now >= slot.local``, and ``(session_date, slot_id)`` has not
    been persisted. Restart after a missed 10:30 therefore fires that slot once
    if the session is still open.
    """
    local_time = now_local.time()
    if local_time < session_open or local_time >= eod:
        return ()
    session_date = now_local.date()
    due: list[ReviewSlot] = []
    for slot in sorted(slots, key=lambda item: item.local):
        if local_time < slot.local:
            continue
        if (session_date, slot.slot_id) in recorded:
            continue
        due.append(slot)
    return tuple(due)
