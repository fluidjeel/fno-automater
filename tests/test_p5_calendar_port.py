"""Phase P5 Calendar Port and Domain Enums Verification Test Suite.

Locks in Phase P5 requirements per FOUR_MODE_LAYER_CHANGE_CRITERIA.md (§1.10)
and NIFTY_FOUR_MODE_CURSOR_REDESIGN.md (§3.3, §3.6):
- ReasonCode enum members: EXPIRY_0_1_DTE_EXCLUDED, CALENDAR_NO_ELIGIBLE_EXPIRY,
  INSTRUMENT_MASTER_ABSENT, preserving enum order and integrity.
- CalendarConfig defaults, verification note, and unverified clocks assertion.
- TradingCalendarPort: session date, holidays, trading days, special sessions,
  session phases (PRE_OPEN, CONTINUOUS, AUCTION, POST_CLOSE, CLOSED, SPECIAL).
- Written skip rule for positional review slots outside special sessions or on holidays.
- Following calendar week range computation and last-Thursday-of-month check.
- Mode 2 following-week expiry selection: 0/1 DTE exclusion, holiday substitution,
  monthly contract substitution, fallback policy, and stable rejection reason codes.
- Integration of TradingCalendarPort into due_review_slots in review_schedule.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from trading.domain.enums import ReasonCode, ReviewSlotId
from trading.identification.calendar import (
    DEFAULT_NSE_2026_HOLIDAYS,
    CalendarConfig,
    M2ExpirySelection,
    SessionPhase,
    SpecialSessionRule,
    TradingCalendarPort,
    get_calendar_port,
)
from trading.runtime.review_schedule import ReviewSlot, due_review_slots

KOLKATA = ZoneInfo("Asia/Kolkata")


def test_reason_code_new_members_and_order() -> None:
    """ReasonCode includes new members while preserving existing members and order."""
    members = list(ReasonCode)

    # Check existence
    assert ReasonCode.EXPIRY_0_1_DTE_EXCLUDED == "EXPIRY_0_1_DTE_EXCLUDED"
    assert ReasonCode.CALENDAR_NO_ELIGIBLE_EXPIRY == "CALENDAR_NO_ELIGIBLE_EXPIRY"
    assert ReasonCode.INSTRUMENT_MASTER_ABSENT == "INSTRUMENT_MASTER_ABSENT"

    # Check relative order
    idx_ok = members.index(ReasonCode.OK)
    idx_mode_family = members.index(ReasonCode.MODE_FAMILY_NOT_PERMITTED)
    idx_0_1_dte = members.index(ReasonCode.EXPIRY_0_1_DTE_EXCLUDED)
    idx_cal_no_exp = members.index(ReasonCode.CALENDAR_NO_ELIGIBLE_EXPIRY)
    idx_inst_absent = members.index(ReasonCode.INSTRUMENT_MASTER_ABSENT)
    idx_risk = members.index(ReasonCode.RISK_LIMIT_TRADE)

    assert (
        idx_ok
        < idx_mode_family
        < idx_0_1_dte
        < idx_cal_no_exp
        < idx_inst_absent
        < idx_risk
    )


def test_calendar_config_defaults() -> None:
    """CalendarConfig has required defaults and verification note."""
    cfg = CalendarConfig()
    assert cfg.calendar_version == "nse-fo-calendar-v1"
    assert "exchange-communication-holidays" in cfg.source
    assert cfg.effective_date == "2026-09-13"
    assert cfg.timezone_name == "Asia/Kolkata"
    assert cfg.session_open == time(9, 15)
    assert cfg.continuous_close == time(15, 30)
    assert cfg.auction_close == time(15, 40)
    assert cfg.entry_cutoff == time(15, 0)
    assert cfg.flatten_cutoff == time(15, 20)
    assert cfg.post_market_close == time(16, 0)
    assert "clocks unverified" in cfg.verification_note
    assert "15:30 / 15:40" in cfg.verification_note


def test_trading_calendar_port_properties_and_clock_verification() -> None:
    """TradingCalendarPort exposes properties and verifies clocks unchanged."""
    port = get_calendar_port()
    assert port.calendar_version == "nse-fo-calendar-v1"
    assert port.effective_date == "2026-09-13"
    assert port.timezone == ZoneInfo("Asia/Kolkata")
    assert port.session_open == time(9, 15)
    assert port.continuous_close == time(15, 30)
    assert port.auction_close == time(15, 40)
    assert port.entry_cutoff == time(15, 0)
    assert port.flatten_cutoff == time(15, 20)
    assert port.verify_clocks_unchanged() is True

    # Mutated clocks fail verification
    custom_cfg = CalendarConfig(continuous_close=time(15, 45))
    custom_port = TradingCalendarPort(config=custom_cfg)
    assert custom_port.verify_clocks_unchanged() is False


def test_session_date_timezone_conversion() -> None:
    """session_date correctly converts naive and aware datetimes to Asia/Kolkata date."""
    port = get_calendar_port()
    # UTC 2026-09-23 20:00 -> IST 2026-09-24 01:30 (+5:30)
    utc_moment = datetime(2026, 9, 23, 20, 0, tzinfo=ZoneInfo("UTC"))
    assert port.session_date(utc_moment) == date(2026, 9, 24)

    # Naive assumed local Asia/Kolkata
    naive_moment = datetime(2026, 9, 24, 10, 0)
    assert port.session_date(naive_moment) == date(2026, 9, 24)


def test_holidays_and_trading_days() -> None:
    """Holidays include weekends and default 2026 NSE holidays."""
    port = get_calendar_port()

    # Weekday trading day
    wednesday = date(2026, 9, 23)
    assert port.is_holiday(wednesday) is False
    assert port.is_trading_day(wednesday) is True

    # Weekend (Saturday & Sunday)
    saturday = date(2026, 9, 26)
    sunday = date(2026, 9, 27)
    assert port.is_holiday(saturday) is True
    assert port.is_trading_day(saturday) is False
    assert port.is_holiday(sunday) is True
    assert port.is_trading_day(sunday) is False

    # Default 2026 holidays
    for hol in DEFAULT_NSE_2026_HOLIDAYS:
        assert port.is_holiday(hol) is True
        assert port.is_trading_day(hol) is False

    # Republic Day 2026-01-26
    assert port.is_holiday(date(2026, 1, 26)) is True
    # Gandhi Jayanti 2026-10-02 (Friday)
    assert port.is_holiday(date(2026, 10, 2)) is True
    assert port.is_trading_day(date(2026, 10, 2)) is False


def test_special_session_is_trading_day() -> None:
    """Special session is recognized as trading day even if on weekend/holiday."""
    muhurat_date = date(2026, 11, 8)  # Sunday
    special_rule = SpecialSessionRule(
        session_date=muhurat_date,
        open_time=time(18, 15),
        close_time=time(19, 15),
        description="Diwali Muhurat Trading",
        skip_standard_reviews=True,
    )
    port = TradingCalendarPort(special_sessions={muhurat_date: special_rule})
    assert port.is_special_session(muhurat_date) is True
    # While it is a holiday/weekend, is_trading_day is True because it has a special session
    assert port.is_trading_day(muhurat_date) is True


def test_session_phase_regular_day() -> None:
    """session_phase classifies PRE_OPEN, CONTINUOUS, AUCTION, POST_CLOSE, CLOSED."""
    port = get_calendar_port()
    sess_date = date(2026, 9, 23)  # Regular Wednesday

    def dt(h: int, m: int) -> datetime:
        return datetime(
            sess_date.year, sess_date.month, sess_date.day, h, m, tzinfo=KOLKATA
        )

    assert port.session_phase(dt(8, 59)) == SessionPhase.CLOSED
    assert port.session_phase(dt(9, 0)) == SessionPhase.PRE_OPEN
    assert port.session_phase(dt(9, 14)) == SessionPhase.PRE_OPEN
    assert port.session_phase(dt(9, 15)) == SessionPhase.CONTINUOUS
    assert port.session_phase(dt(11, 30)) == SessionPhase.CONTINUOUS
    assert port.session_phase(dt(15, 29)) == SessionPhase.CONTINUOUS
    assert port.session_phase(dt(15, 30)) == SessionPhase.AUCTION
    assert port.session_phase(dt(15, 39)) == SessionPhase.AUCTION
    assert port.session_phase(dt(15, 40)) == SessionPhase.POST_CLOSE
    assert port.session_phase(dt(15, 59)) == SessionPhase.POST_CLOSE
    assert port.session_phase(dt(16, 0)) == SessionPhase.CLOSED
    assert port.session_phase(dt(18, 0)) == SessionPhase.CLOSED


def test_session_phase_holiday_and_special_session() -> None:
    """session_phase returns CLOSED on holiday and SPECIAL inside special session hours."""
    muhurat_date = date(2026, 11, 8)
    special_rule = SpecialSessionRule(
        session_date=muhurat_date,
        open_time=time(18, 15),
        close_time=time(19, 15),
        description="Muhurat",
    )
    port = TradingCalendarPort(special_sessions={muhurat_date: special_rule})

    # Regular holiday without special session
    hol_moment = datetime(2026, 1, 26, 11, 0, tzinfo=KOLKATA)
    assert port.session_phase(hol_moment) == SessionPhase.CLOSED

    # Special session date: before open -> CLOSED
    assert (
        port.session_phase(datetime(2026, 11, 8, 10, 30, tzinfo=KOLKATA))
        == SessionPhase.CLOSED
    )
    # Inside hours -> SPECIAL
    assert (
        port.session_phase(datetime(2026, 11, 8, 18, 30, tzinfo=KOLKATA))
        == SessionPhase.SPECIAL
    )
    # After close -> CLOSED
    assert (
        port.session_phase(datetime(2026, 11, 8, 19, 20, tzinfo=KOLKATA))
        == SessionPhase.CLOSED
    )


def test_should_skip_positional_review() -> None:
    """Written skip rule (§1.10) skips reviews on holidays and outside special session hours."""
    muhurat_date = date(2026, 11, 8)
    special_rule = SpecialSessionRule(
        session_date=muhurat_date,
        open_time=time(18, 15),
        close_time=time(19, 15),
        description="Muhurat",
        skip_standard_reviews=True,
    )
    port = TradingCalendarPort(special_sessions={muhurat_date: special_rule})

    # Holiday (non-trading day) skips review
    assert (
        port.should_skip_positional_review(
            datetime(2026, 1, 26, 10, 30, tzinfo=KOLKATA), time(10, 30)
        )
        is True
    )

    # Special session outside trading hours (10:30 and 14:30 outside 18:15-19:15) skips
    assert (
        port.should_skip_positional_review(
            datetime(2026, 11, 8, 10, 30, tzinfo=KOLKATA), time(10, 30)
        )
        is True
    )
    assert (
        port.should_skip_positional_review(
            datetime(2026, 11, 8, 14, 30, tzinfo=KOLKATA), time(14, 30)
        )
        is True
    )

    # Regular trading day does NOT skip
    regular_moment = datetime(2026, 9, 24, 10, 30, tzinfo=KOLKATA)
    assert port.should_skip_positional_review(regular_moment, time(10, 30)) is False
    assert port.should_skip_positional_review(regular_moment, time(14, 30)) is False


def test_following_calendar_week_range() -> None:
    """following_calendar_week_range returns Monday and Sunday of next calendar week."""
    port = get_calendar_port()

    # From Thursday 2026-09-24:
    # This week Monday is 2026-09-21. Next week is Mon 2026-09-28 to Sun 2026-10-04.
    mon, sun = port.following_calendar_week_range(date(2026, 9, 24))
    assert mon == date(2026, 9, 28)
    assert sun == date(2026, 10, 4)

    # From Monday 2026-09-21:
    mon, sun = port.following_calendar_week_range(date(2026, 9, 21))
    assert mon == date(2026, 9, 28)
    assert sun == date(2026, 10, 4)

    # From Sunday 2026-09-27:
    mon, sun = port.following_calendar_week_range(date(2026, 9, 27))
    assert mon == date(2026, 9, 28)
    assert sun == date(2026, 10, 4)


def test_is_last_thursday_of_month() -> None:
    """is_last_thursday_of_month correctly detects monthly expiry Thursday."""
    port = get_calendar_port()

    # September 2026:
    # 2026-09-17 (Thursday, next week is Sep 24 -> same month)
    assert port.is_last_thursday_of_month(date(2026, 9, 17)) is False

    # 2026-09-24 (Thursday, next week is Oct 01 -> different month)
    assert port.is_last_thursday_of_month(date(2026, 9, 24)) is True

    # Not a Thursday (Wednesday 2026-09-23)
    assert port.is_last_thursday_of_month(date(2026, 9, 23)) is False


def test_select_m2_expiry_normal_weekly() -> None:
    """select_m2_expiry selects following week Thursday with ReasonCode.OK."""
    port = get_calendar_port()
    as_of = date(2026, 9, 24)  # Thursday
    # Following week is Sep 28 - Oct 04. Thursday is Oct 01.
    listed = [date(2026, 9, 24), date(2026, 10, 1), date(2026, 10, 8)]

    selection = port.select_m2_expiry(listed, as_of=as_of)
    assert isinstance(selection, M2ExpirySelection)
    assert selection.eligible is True
    assert selection.selected_expiry == date(2026, 10, 1)
    assert selection.dte == 7
    assert selection.target_week_start == date(2026, 9, 28)
    assert selection.target_week_end == date(2026, 10, 4)
    assert selection.is_holiday_substituted is False
    assert selection.reason_code == ReasonCode.OK


def test_select_m2_expiry_holiday_substitution() -> None:
    """select_m2_expiry detects holiday substitution when Thursday is a holiday."""
    # Suppose Thursday 2026-10-01 is a holiday, exchange lists Wednesday 2026-09-30
    custom_holidays = DEFAULT_NSE_2026_HOLIDAYS | {date(2026, 10, 1)}
    port = TradingCalendarPort(holidays=custom_holidays)
    as_of = date(2026, 9, 24)
    listed = [date(2026, 9, 30), date(2026, 10, 8)]

    selection = port.select_m2_expiry(listed, as_of=as_of)
    assert selection.eligible is True
    assert selection.selected_expiry == date(2026, 9, 30)
    assert selection.is_holiday_substituted is True
    assert (
        "Holiday substitution: Thursday 2026-10-01 is holiday" in selection.audit_note
    )
    assert "selected listed expiry 2026-09-30" in selection.audit_note


def test_select_m2_expiry_monthly_substitution() -> None:
    """select_m2_expiry marks monthly substitution when contract is last Thursday of month."""
    port = get_calendar_port()
    # As of Thursday 2026-09-17: following week is Sep 21 - Sep 27. Thursday is Sep 24.
    # Sep 24 is last Thursday of September 2026 -> monthly expiry!
    as_of = date(2026, 9, 17)
    listed = [date(2026, 9, 17), date(2026, 9, 24), date(2026, 10, 1)]

    selection = port.select_m2_expiry(listed, as_of=as_of)
    assert selection.eligible is True
    assert selection.selected_expiry == date(2026, 9, 24)
    assert selection.is_monthly_substituted is True
    assert (
        "Monthly contract fulfills following-week role: 2026-09-24"
        in selection.audit_note
    )


def test_select_m2_expiry_0_1_dte_excluded() -> None:
    """select_m2_expiry returns EXPIRY_0_1_DTE_EXCLUDED when only 0/1 DTE available."""
    port = get_calendar_port()
    as_of = date(2026, 9, 24)
    # Only 0 DTE (today) and 1 DTE (tomorrow) are listed; no following week expiry
    listed = [date(2026, 9, 24), date(2026, 9, 25)]

    selection = port.select_m2_expiry(listed, as_of=as_of)
    assert selection.eligible is False
    assert selection.selected_expiry is None
    assert selection.reason_code == ReasonCode.EXPIRY_0_1_DTE_EXCLUDED
    assert "Candidate expiries excluded by 0/1 DTE ban" in selection.audit_note


def test_select_m2_expiry_no_eligible_expiry() -> None:
    """select_m2_expiry returns CALENDAR_NO_ELIGIBLE_EXPIRY when no following week expiry."""
    port = get_calendar_port()
    as_of = date(2026, 9, 24)
    # Master is empty or has only past expiries
    listed = [date(2026, 9, 10)]

    selection = port.select_m2_expiry(listed, as_of=as_of)
    assert selection.eligible is False
    assert selection.selected_expiry is None
    assert selection.reason_code == ReasonCode.CALENDAR_NO_ELIGIBLE_EXPIRY
    assert (
        "No eligible listed expiry found in following calendar week"
        in selection.audit_note
    )


def test_select_m2_expiry_fallback_policy() -> None:
    """select_m2_expiry allows selecting subsequent expiry when allow_fallback=True."""
    port = get_calendar_port()
    as_of = date(2026, 9, 24)
    # No expiry in target week Sep 28 - Oct 04, but Oct 08 is listed
    listed = [date(2026, 9, 24), date(2026, 10, 8)]

    # Disallowed fallback -> CALENDAR_NO_ELIGIBLE_EXPIRY
    no_fallback = port.select_m2_expiry(listed, as_of=as_of, allow_fallback=False)
    # Note: 2026-09-24 is 0 DTE so has_0_1_dte triggers EXPIRY_0_1_DTE_EXCLUDED
    assert no_fallback.eligible is False
    assert no_fallback.reason_code == ReasonCode.EXPIRY_0_1_DTE_EXCLUDED

    # Allowed fallback -> Selects 2026-10-08
    with_fallback = port.select_m2_expiry(listed, as_of=as_of, allow_fallback=True)
    assert with_fallback.eligible is True
    assert with_fallback.selected_expiry == date(2026, 10, 8)
    assert "Fallback expiry selected: 2026-10-08" in with_fallback.audit_note


def test_due_review_slots_with_calendar_skip() -> None:
    """due_review_slots skips slots when calendar indicates positional review should be skipped."""
    muhurat_date = date(2026, 11, 8)
    special_rule = SpecialSessionRule(
        session_date=muhurat_date,
        open_time=time(18, 15),
        close_time=time(19, 15),
        description="Muhurat",
        skip_standard_reviews=True,
    )
    port = TradingCalendarPort(special_sessions={muhurat_date: special_rule})

    slots = (
        ReviewSlot(ReviewSlotId.NSE_MORNING, time(10, 30)),
        ReviewSlot(ReviewSlotId.NSE_AFTERNOON, time(14, 30)),
    )

    # On Muhurat Sunday at 11:00:
    # Without calendar, 10:30 would be due if within session_open/eod
    due_no_cal = due_review_slots(
        now_local=datetime(2026, 11, 8, 11, 0),
        session_open=time(9, 15),
        eod=time(15, 30),
        slots=slots,
        recorded=frozenset(),
        calendar=None,
    )
    assert len(due_no_cal) == 1
    assert due_no_cal[0].slot_id == ReviewSlotId.NSE_MORNING

    # With calendar, 10:30 is skipped because it's outside the Muhurat session
    due_with_cal = due_review_slots(
        now_local=datetime(2026, 11, 8, 11, 0),
        session_open=time(9, 15),
        eod=time(15, 30),
        slots=slots,
        recorded=frozenset(),
        calendar=port,
    )
    assert due_with_cal == ()

    # On regular trading day 2026-09-24 at 11:00 with calendar, 10:30 is DUE
    due_reg = due_review_slots(
        now_local=datetime(2026, 9, 24, 11, 0),
        session_open=time(9, 15),
        eod=time(15, 30),
        slots=slots,
        recorded=frozenset(),
        calendar=port,
    )
    assert len(due_reg) == 1
    assert due_reg[0].slot_id == ReviewSlotId.NSE_MORNING
