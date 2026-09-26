"""Following-week supplemental chain selection when the near chain is preloaded."""

from __future__ import annotations

from datetime import UTC, date, datetime

from trading.data.events import CanonicalMarketEvent
from trading.identification.calendar import get_calendar_port
from trading.runtime.m2_chain import following_week_epoch


def _chain_payload() -> dict[str, object]:
    return {
        "expiry_data": [
            {"date": "29-09-2026", "expiry": "1790676600"},
            {"date": "06-10-2026", "expiry": "1791281400"},
            {"date": "13-10-2026", "expiry": "1791886200"},
        ],
        "strikes": [],
    }


def _chain() -> CanonicalMarketEvent:
    return CanonicalMarketEvent(
        event_id="evt-1",
        event_type="OPTION_CHAIN_SNAPSHOT",
        symbol="NSE:NIFTY50-INDEX",
        event_time=datetime(2026, 9, 25, 4, 0, tzinfo=UTC),
        receive_time=datetime(2026, 9, 25, 4, 0, tzinfo=UTC),
        source_time=datetime(2026, 9, 25, 4, 0, tzinfo=UTC),
        provider="fyers",
        provider_sequence=1,
        normalization_version="1",
        raw_ref="test",
        payload=_chain_payload(),
    )


def test_following_week_epoch_returns_calendar_pick_when_not_loaded() -> None:
    """First supplemental fetch uses the M2 calendar expiry."""
    epoch = following_week_epoch(
        _chain(),
        as_of=date(2026, 9, 25),
        calendar=get_calendar_port(),
        already_listed=set(),
    )
    assert epoch == 1790676600


def test_following_week_epoch_advances_when_calendar_pick_already_loaded() -> None:
    """When the default chain is already 29-Sep, load 06-Oct for M3/M4 DTE."""
    epoch = following_week_epoch(
        _chain(),
        as_of=date(2026, 9, 25),
        calendar=get_calendar_port(),
        already_listed={date(2026, 9, 29)},
    )
    assert epoch == 1791281400


def test_following_week_epoch_returns_none_when_no_further_expiries() -> None:
    """No supplemental fetch when every later expiry is already listed."""
    epoch = following_week_epoch(
        _chain(),
        as_of=date(2026, 9, 25),
        calendar=get_calendar_port(),
        already_listed={
            date(2026, 9, 29),
            date(2026, 10, 6),
            date(2026, 10, 13),
        },
    )
    assert epoch is None
