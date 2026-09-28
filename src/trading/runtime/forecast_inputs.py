"""Load pipeline-produced reference rows for the forecast stage.

Files live under ``data/reference/``. Each is optional JSON Lines; a missing
file yields no rows and the forecasters record the feature as absent. A
malformed row is skipped, never repaired.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypeVar

from trading.domain.clock import ensure_utc
from trading.forecast.reference import (
    CalendarEvent,
    GlobalMacroRow,
    ParticipantFlow,
    PremarketCues,
    ReferenceInputs,
)

__all__ = [
    "EVENT_CALENDAR_FILE",
    "GLOBAL_MACRO_FILE",
    "PARTICIPANT_FLOWS_FILE",
    "PREMARKET_CUES_FILE",
    "ReferenceInputLoader",
]

PREMARKET_CUES_FILE = "premarket_cues.jsonl"
PARTICIPANT_FLOWS_FILE = "participant_flows.jsonl"
GLOBAL_MACRO_FILE = "global_macro.jsonl"
EVENT_CALENDAR_FILE = "event_calendar.jsonl"

_T = TypeVar("_T")
_LOG = logging.getLogger(__name__)


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool | float):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _rows(path: Path, parse: Callable[[dict[str, Any]], _T]) -> tuple[_T, ...]:
    if not path.exists():
        return ()
    out: list[_T] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line, parse_float=Decimal)
            if isinstance(payload, dict):
                out.append(parse(payload))
        except (ValueError, KeyError, TypeError) as exc:
            _LOG.warning("skipping malformed reference row in %s: %s", path.name, exc)
    return tuple(out)


def _premarket(row: dict[str, Any]) -> PremarketCues:
    return PremarketCues(
        as_of=ensure_utc(datetime.fromisoformat(str(row["as_of"]))),
        gift_nifty_change=_dec(row.get("gift_nifty_change")),
        us_futures_change=_dec(row.get("us_futures_change")),
        usdinr_change=_dec(row.get("usdinr_change")),
        crude_change=_dec(row.get("crude_change")),
    )


def _flow(row: dict[str, Any]) -> ParticipantFlow:
    return ParticipantFlow(
        session=date.fromisoformat(str(row["session"])),
        fii_index_futures_long=_dec(row.get("fii_index_futures_long")),
        fii_index_futures_short=_dec(row.get("fii_index_futures_short")),
        fii_cash_net=_dec(row.get("fii_cash_net")),
        dii_cash_net=_dec(row.get("dii_cash_net")),
    )


def _global(row: dict[str, Any]) -> GlobalMacroRow:
    return GlobalMacroRow(
        session=date.fromisoformat(str(row["session"])),
        us10y_yield=_dec(row.get("us10y_yield")),
        dxy=_dec(row.get("dxy")),
        crude=_dec(row.get("crude")),
        usdinr=_dec(row.get("usdinr")),
        spx=_dec(row.get("spx")),
    )


def _event(row: dict[str, Any]) -> CalendarEvent:
    return CalendarEvent(
        event_date=date.fromisoformat(str(row["event_date"])),
        kind=str(row["kind"]),
        importance=int(row["importance"]),
        typical_move_fraction=_dec(row.get("typical_move_fraction")),
    )


class ReferenceInputLoader:
    """Reload reference files only when their modification time changes."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._stamp: tuple[float, ...] | None = None
        self._cached = ReferenceInputs()

    def _mtimes(self) -> tuple[float, ...]:
        names = (
            PREMARKET_CUES_FILE,
            PARTICIPANT_FLOWS_FILE,
            GLOBAL_MACRO_FILE,
            EVENT_CALENDAR_FILE,
        )
        return tuple(
            (self._directory / name).stat().st_mtime
            if (self._directory / name).exists()
            else -1.0
            for name in names
        )

    def load(self) -> ReferenceInputs:
        """Current reference bundle; the latest pre-market row wins."""
        stamp = self._mtimes()
        if stamp == self._stamp:
            return self._cached
        premarket = _rows(self._directory / PREMARKET_CUES_FILE, _premarket)
        flows = sorted(
            _rows(self._directory / PARTICIPANT_FLOWS_FILE, _flow),
            key=lambda row: row.session,
        )
        global_rows = sorted(
            _rows(self._directory / GLOBAL_MACRO_FILE, _global),
            key=lambda row: row.session,
        )
        calendar_path = self._directory / EVENT_CALENDAR_FILE
        events = sorted(_rows(calendar_path, _event), key=lambda row: row.event_date)
        self._cached = ReferenceInputs(
            premarket=max(premarket, key=lambda row: row.as_of) if premarket else None,
            flows=tuple(flows),
            global_macro=tuple(global_rows),
            events=tuple(events),
            events_loaded=calendar_path.exists(),
        )
        self._stamp = stamp
        return self._cached
