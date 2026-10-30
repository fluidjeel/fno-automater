"""IST session date and five-minute platter clock marks."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")
_SESSION_OPEN = time(9, 15)
_SESSION_CLOSE = time(15, 30)


def ist_session_date(now: datetime) -> date:
    """Calendar session date in IST for regular cash hours."""
    local = now.astimezone(_IST)
    if local.time() < _SESSION_OPEN:
        return local.date()
    return local.date()


def platter_marks_for_session(session_date: date) -> tuple[datetime, ...]:
    """Return 09:15–15:30 IST marks every five minutes (76 marks)."""
    marks: list[datetime] = []
    start = datetime.combine(session_date, _SESSION_OPEN, tzinfo=_IST)
    end = datetime.combine(session_date, _SESSION_CLOSE, tzinfo=_IST)
    mark = start
    while mark <= end:
        marks.append(mark)
        mark += timedelta(minutes=5)
    return tuple(marks)


def next_platter_mark_ist(now: datetime) -> datetime | None:
    """Next grid mark at or after `now`, or None after session close."""
    session = ist_session_date(now)
    local = now.astimezone(_IST)
    for mark in platter_marks_for_session(session):
        if mark >= local.replace(second=0, microsecond=0):
            return mark
    return None
