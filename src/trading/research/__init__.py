"""Research-only strategy families kept off the strict bounded-risk book."""

from trading.research.registry import (
    CALENDAR_FAMILIES,
    family_research_status,
    is_calendar_family,
    is_experimental_off_strict_book,
    refuses_same_expiry_payoff,
)

__all__ = [
    "CALENDAR_FAMILIES",
    "family_research_status",
    "is_calendar_family",
    "is_experimental_off_strict_book",
    "refuses_same_expiry_payoff",
]
