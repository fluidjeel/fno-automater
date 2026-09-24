"""Authoritative exchange calendar port and session phase classification.

Provides session date, phase, listed expiries, holidays, entry cutoff,
flatten cutoff, source, effective date, and M2 contract expiry selection.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum, unique
from zoneinfo import ZoneInfo

from trading.domain.enums import ReasonCode

__all__ = [
    "DEFAULT_NSE_2026_HOLIDAYS",
    "SATURDAY_WEEKDAY",
    "THURSDAY_WEEKDAY",
    "CalendarConfig",
    "M2ExpirySelection",
    "SessionPhase",
    "SpecialSessionRule",
    "TradingCalendarPort",
    "get_calendar_port",
]

SATURDAY_WEEKDAY: int = 5
THURSDAY_WEEKDAY: int = 3
DAYS_IN_WEEK: int = 7
DAYS_TO_THURSDAY: int = 3
DAYS_TO_SUNDAY: int = 6

EXPIRY_0_1_DTE_NOTE: str = (
    "Candidate expiries excluded by 0/1 DTE ban and no following-week "
    "contract available"
)
NO_ELIGIBLE_EXPIRY_NOTE: str = (
    "No eligible listed expiry found in following calendar week"
)


@unique
class SessionPhase(StrEnum):
    """Trading session phase classification."""

    PRE_OPEN = "PRE_OPEN"
    CONTINUOUS = "CONTINUOUS"
    AUCTION = "AUCTION"
    POST_CLOSE = "POST_CLOSE"
    CLOSED = "CLOSED"
    SPECIAL = "SPECIAL"


@dataclass(frozen=True, slots=True)
class SpecialSessionRule:
    """Configuration for a non-standard exchange session."""

    session_date: date
    open_time: time
    close_time: time
    description: str
    skip_standard_reviews: bool = True


@dataclass(frozen=True, slots=True)
class M2ExpirySelection:
    """Result of Mode 2 following-week expiry selection."""

    eligible: bool
    selected_expiry: date | None
    as_of: date
    dte: int | None
    target_week_start: date
    target_week_end: date
    is_holiday_substituted: bool = False
    is_monthly_substituted: bool = False
    reason_code: ReasonCode = ReasonCode.OK
    audit_note: str = ""


@dataclass(frozen=True, slots=True)
class CalendarConfig:
    """Exchange session clock parameters and metadata."""

    calendar_version: str = "nse-fo-calendar-v1"
    source: str = (
        "https://www.nseindia.com/resources/exchange-communication-holidays-price-bands"
    )
    effective_date: str = "2026-09-13"
    timezone_name: str = "Asia/Kolkata"
    session_open: time = time(9, 15)
    continuous_close: time = time(15, 30)
    auction_close: time = time(15, 40)
    entry_cutoff: time = time(15, 0)
    flatten_cutoff: time = time(15, 20)
    post_market_close: time = time(16, 0)
    verification_note: str = (
        "clocks unverified with live broker; keeping official 15:30 / 15:40 until "
        "external circular and broker check are formally recorded"
    )


DEFAULT_NSE_2026_HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2026, 1, 26),  # Republic Day
        date(2026, 2, 17),  # Mahashivratri
        date(2026, 3, 3),  # Holi
        date(2026, 3, 20),  # Eid-ul-Fitr
        date(2026, 4, 3),  # Good Friday
        date(2026, 4, 14),  # Ambedkar Jayanti
        date(2026, 8, 15),  # Independence Day
        date(2026, 10, 2),  # Mahatma Gandhi Jayanti
        date(2026, 10, 20),  # Dussehra
        date(2026, 11, 8),  # Diwali
        date(2026, 11, 24),  # Gurunanak Jayanti
        date(2026, 12, 25),  # Christmas
    }
)


class TradingCalendarPort:
    """Authoritative exchange calendar port."""

    def __init__(
        self,
        config: CalendarConfig | None = None,
        holidays: frozenset[date] | None = None,
        special_sessions: dict[date, SpecialSessionRule] | None = None,
    ) -> None:
        self._config = config if config is not None else CalendarConfig()
        self._holidays = holidays if holidays is not None else DEFAULT_NSE_2026_HOLIDAYS
        self._special_sessions = (
            dict(special_sessions) if special_sessions is not None else {}
        )
        self._tz = ZoneInfo(self._config.timezone_name)

    @property
    def calendar_version(self) -> str:
        return self._config.calendar_version

    @property
    def source(self) -> str:
        return self._config.source

    @property
    def effective_date(self) -> str:
        return self._config.effective_date

    @property
    def verification_note(self) -> str:
        return self._config.verification_note

    @property
    def timezone(self) -> ZoneInfo:
        return self._tz

    @property
    def session_open(self) -> time:
        return self._config.session_open

    @property
    def continuous_close(self) -> time:
        return self._config.continuous_close

    @property
    def auction_close(self) -> time:
        return self._config.auction_close

    @property
    def entry_cutoff(self) -> time:
        return self._config.entry_cutoff

    @property
    def flatten_cutoff(self) -> time:
        return self._config.flatten_cutoff

    def verify_clocks_unchanged(self) -> bool:
        """Check continuous and auction closing times match official 15:30 / 15:40."""
        return self.continuous_close == time(15, 30) and self.auction_close == time(
            15, 40
        )

    def session_date(self, moment: datetime) -> date:
        """Return the calendar session date in Asia/Kolkata."""
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=self._tz)
        return moment.astimezone(self._tz).date()

    def is_holiday(self, d: date) -> bool:
        """Return True if date is a weekend or listed exchange holiday."""
        return d.weekday() >= SATURDAY_WEEKDAY or d in self._holidays

    def is_trading_day(self, d: date) -> bool:
        """Return True if date is a special session or not a holiday."""
        return d in self._special_sessions or not self.is_holiday(d)

    def is_special_session(self, d: date) -> bool:
        """Return True if date is registered as a special session."""
        return d in self._special_sessions

    def session_phase(self, moment: datetime) -> SessionPhase:
        """Evaluate session phase in Asia/Kolkata.

        If special session on that date, returns SPECIAL if inside hours, else CLOSED.
        Otherwise, if not trading day, CLOSED. Else maps to:
          PRE_OPEN: 09:00 - 09:15
          CONTINUOUS: 09:15 - 15:30
          AUCTION: 15:30 - 15:40
          POST_CLOSE: 15:40 - 16:00
          CLOSED: otherwise
        """
        if moment.tzinfo is None:
            local_moment = moment.replace(tzinfo=self._tz)
        else:
            local_moment = moment.astimezone(self._tz)

        sess_date = local_moment.date()
        local_time = local_moment.time()

        if self.is_special_session(sess_date):
            rule = self._special_sessions[sess_date]
            if rule.open_time <= local_time < rule.close_time:
                return SessionPhase.SPECIAL
            return SessionPhase.CLOSED

        if not self.is_trading_day(sess_date):
            return SessionPhase.CLOSED

        phase = SessionPhase.CLOSED
        if time(9, 0) <= local_time < self.session_open:
            phase = SessionPhase.PRE_OPEN
        elif self.session_open <= local_time < self.continuous_close:
            phase = SessionPhase.CONTINUOUS
        elif self.continuous_close <= local_time < self.auction_close:
            phase = SessionPhase.AUCTION
        elif self.auction_close <= local_time < self._config.post_market_close:
            phase = SessionPhase.POST_CLOSE
        return phase

    def should_skip_positional_review(self, moment: datetime, slot_time: time) -> bool:
        """Written skip rule (§1.10).

        If the date is a special session, and the special session's trading hours
        do not contain slot_time (e.g. 10:30 and 14:30 are outside an evening Muhurat
        session), return True (skip). Also if not a trading day, return True.
        """
        d = self.session_date(moment)
        if not self.is_trading_day(d):
            return True
        if self.is_special_session(d):
            rule = self._special_sessions[d]
            if not (rule.open_time <= slot_time < rule.close_time):
                return True
            if rule.skip_standard_reviews:
                return True
        return False

    def following_calendar_week_range(self, as_of: date) -> tuple[date, date]:
        """Compute Monday and Sunday of the calendar week following as_of."""
        monday_this_week = as_of - timedelta(days=as_of.weekday())
        monday_next_week = monday_this_week + timedelta(days=DAYS_IN_WEEK)
        sunday_next_week = monday_next_week + timedelta(days=DAYS_TO_SUNDAY)
        return (monday_next_week, sunday_next_week)

    def is_last_thursday_of_month(self, d: date) -> bool:
        """Return True if d is Thursday and next week falls in a different month."""
        return (
            d.weekday() == THURSDAY_WEEKDAY
            and (d + timedelta(days=DAYS_IN_WEEK)).month != d.month
        )

    def _is_monthly_contract(self, d: date) -> bool:
        """Return True if contract fulfills monthly expiry role."""
        return self.is_last_thursday_of_month(d) or (
            d.weekday() != THURSDAY_WEEKDAY
            and (d + timedelta(days=DAYS_IN_WEEK)).month != d.month
        )

    def select_m2_expiry(
        self,
        listed_expiries: Iterable[date],
        as_of: date,
        *,
        allow_fallback: bool = False,
    ) -> M2ExpirySelection:
        """Select the following calendar week expiry from listed expiries from master.

        1. Exclude all expiries where (exp - as_of).days <= 1 (0/1 DTE ban).
        2. Filter eligible listed expiries falling in following calendar week.
        3. If found in following week:
           - Check holiday substitution (expected Thursday is holiday).
           - Check monthly substitution.
           - Return M2ExpirySelection.
        4. If no eligible listed expiry in following week:
           - If allow_fallback: find nearest subsequent eligible listed expiry.
           - Else if excluded solely due to 0/1 DTE, return EXPIRY_0_1_DTE_EXCLUDED.
           - Else return CALENDAR_NO_ELIGIBLE_EXPIRY.
        """
        all_expiries = sorted(set(listed_expiries))
        monday, sunday = self.following_calendar_week_range(as_of)

        eligible_expiries = [exp for exp in all_expiries if (exp - as_of).days > 1]
        following_week_candidates = [
            exp for exp in eligible_expiries if monday <= exp <= sunday
        ]

        if following_week_candidates:
            expected_thursday = monday + timedelta(days=DAYS_TO_THURSDAY)
            if expected_thursday in following_week_candidates:
                selected_expiry = expected_thursday
            else:
                selected_expiry = following_week_candidates[0]

            is_holiday_sub = False
            audit_note = ""
            if self.is_holiday(expected_thursday):
                is_holiday_sub = True
                audit_note = (
                    f"Holiday substitution: Thursday {expected_thursday} is holiday; "
                    f"selected listed expiry {selected_expiry}"
                )

            is_monthly_sub = False
            if self._is_monthly_contract(selected_expiry):
                is_monthly_sub = True
                monthly_msg = (
                    f"Monthly contract fulfills following-week role: {selected_expiry}"
                )
                audit_note = (audit_note + " | " if audit_note else "") + monthly_msg

            return M2ExpirySelection(
                eligible=True,
                selected_expiry=selected_expiry,
                as_of=as_of,
                dte=(selected_expiry - as_of).days,
                target_week_start=monday,
                target_week_end=sunday,
                is_holiday_substituted=is_holiday_sub,
                is_monthly_substituted=is_monthly_sub,
                reason_code=ReasonCode.OK,
                audit_note=audit_note,
            )

        if allow_fallback:
            later_candidates = [exp for exp in eligible_expiries if exp > sunday]
            if later_candidates:
                selected_expiry = later_candidates[0]
                is_monthly_sub = self._is_monthly_contract(selected_expiry)
                audit_note = (
                    f"Fallback expiry selected: {selected_expiry} "
                    f"(target week {monday} to {sunday} had no eligible listed expiry)"
                )
                if is_monthly_sub:
                    audit_note += (
                        f" | Monthly contract fulfills fallback role: {selected_expiry}"
                    )
                return M2ExpirySelection(
                    eligible=True,
                    selected_expiry=selected_expiry,
                    as_of=as_of,
                    dte=(selected_expiry - as_of).days,
                    target_week_start=monday,
                    target_week_end=sunday,
                    is_holiday_substituted=False,
                    is_monthly_substituted=is_monthly_sub,
                    reason_code=ReasonCode.OK,
                    audit_note=audit_note,
                )

        has_0_1_dte = any(0 <= (exp - as_of).days <= 1 for exp in all_expiries)
        if has_0_1_dte:
            return M2ExpirySelection(
                eligible=False,
                selected_expiry=None,
                as_of=as_of,
                dte=None,
                target_week_start=monday,
                target_week_end=sunday,
                reason_code=ReasonCode.EXPIRY_0_1_DTE_EXCLUDED,
                audit_note=EXPIRY_0_1_DTE_NOTE,
            )

        return M2ExpirySelection(
            eligible=False,
            selected_expiry=None,
            as_of=as_of,
            dte=None,
            target_week_start=monday,
            target_week_end=sunday,
            reason_code=ReasonCode.CALENDAR_NO_ELIGIBLE_EXPIRY,
            audit_note=NO_ELIGIBLE_EXPIRY_NOTE,
        )


def get_calendar_port(
    config: CalendarConfig | None = None,
) -> TradingCalendarPort:
    """Factory creating a TradingCalendarPort with given or default config."""
    return TradingCalendarPort(config=config)
