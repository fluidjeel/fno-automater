"""Shared G1/G2 and experimental family gate constants."""

from __future__ import annotations

from trading.domain.enums import FamilyId

__all__ = [
    "CALENDAR_FAMILIES",
    "G1_EXCEEDS_BUDGET_FAMILIES",
    "G2_UNPROVEN_FAMILIES",
]

G1_EXCEEDS_BUDGET_FAMILIES: frozenset[str] = frozenset(
    {
        "long_straddle",
        "long_strangle",
    }
)

G2_UNPROVEN_FAMILIES: frozenset[str] = frozenset(
    {
        "defined_risk_multileg",
        "iron_condor",
        "credit_spread",
    }
)

CALENDAR_FAMILIES: frozenset[str] = frozenset(
    {
        FamilyId.long_call_calendar.value,
        FamilyId.long_put_calendar.value,
    }
)
