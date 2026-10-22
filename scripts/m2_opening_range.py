from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
OPEN_START = time(9, 15)
OPEN_END = time(9, 35)
OPEN_EVAL = time(9, 50)


@dataclass(frozen=True, slots=True)
class OpeningRange:
    """Per session/symbol bid range in the first opening window."""

    session: date
    symbol: str
    min_bid: Decimal | None
    max_bid: Decimal | None
    observation_count: int


def build_opening_ranges(
    snaps: list[dict],
    symbols: set[str],
) -> dict[tuple[date, str], OpeningRange]:
    """Aggregate min/max bid in 09:15-09:35 IST per symbol per session."""
    mins: dict[tuple[date, str], Decimal] = {}
    maxs: dict[tuple[date, str], Decimal] = {}
    counts: dict[tuple[date, str], int] = {}
    for snap in snaps:
        at = snap["at"].astimezone(IST)
        local_t = at.time()
        if local_t < OPEN_START or local_t > OPEN_END:
            continue
        sess = at.date()
        for sym in symbols:
            q = snap["quotes"].get(sym)
            if q is None:
                continue
            bid = q.get("bid")
            if bid is None:
                continue
            key = (sess, sym)
            counts[key] = counts.get(key, 0) + 1
            mins[key] = min(mins.get(key, bid), bid)
            maxs[key] = max(maxs.get(key, bid), bid)
    out: dict[tuple[date, str], OpeningRange] = {}
    for key, n in counts.items():
        out[key] = OpeningRange(
            session=key[0],
            symbol=key[1],
            min_bid=mins.get(key),
            max_bid=maxs.get(key),
            observation_count=n,
        )
    return out


def opening_eval_instant(session: date) -> datetime:
    """Model stop evaluation at 09:50 IST on the session date."""
    return datetime.combine(session, OPEN_EVAL, tzinfo=IST)
