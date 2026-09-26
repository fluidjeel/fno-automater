"""Following-week NIFTY chain selection from provider expiry rows."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from trading.data.events import CanonicalMarketEvent
from trading.identification.calendar import TradingCalendarPort

__all__ = ["expiry_epochs", "following_week_epoch"]


_DATE_PARTS = 3
_YEAR_DIGITS = 4


def expiry_epochs(payload: dict[str, object]) -> dict[date, int]:
    """Map listed expiry dates to provider epochs. Absent epochs are omitted."""
    rows = payload.get("expiry_data")
    if not isinstance(rows, list):
        rows = payload.get("expiryData")
    if not isinstance(rows, list):
        return {}
    found: dict[date, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        parsed = _parse_date(row.get("date"))
        epoch = _parse_epoch(row.get("epoch", row.get("expiry")))
        if parsed is None or epoch is None:
            continue
        found[parsed] = epoch
    return found


def following_week_epoch(
    chain: CanonicalMarketEvent,
    *,
    as_of: date,
    calendar: TradingCalendarPort,
    already_listed: set[date],
) -> int | None:
    """Epoch of the supplemental chain when the calendar pick is already loaded."""
    epochs = expiry_epochs(chain.payload)
    if not epochs:
        return None
    selection = calendar.select_m2_expiry(
        epochs, as_of=as_of, allow_fallback=True
    )
    selected = selection.selected_expiry
    if selected is None or not selection.eligible:
        return None
    if selected not in already_listed:
        return epochs.get(selected)
    # Default Fyers chain often equals the calendar following-week expiry. Advance
    # to the next listed expiry so M3/M4 can bind weekly_dte_min+ contracts.
    for expiry in sorted(epochs):
        if expiry in already_listed:
            continue
        if (expiry - as_of).days <= 1:
            continue
        return epochs.get(expiry)
    return None


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    parts = value.split("-")
    if len(parts) != _DATE_PARTS or not all(part.isdigit() for part in parts):
        return None
    if len(parts[0]) == _YEAR_DIGITS:
        year, month, day = (int(part) for part in parts)
    elif len(parts[2]) == _YEAR_DIGITS:
        day, month, year = (int(part) for part in parts)
    else:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_epoch(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, Decimal):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
