"""Research registry for families not admitted to the strict bounded-risk book."""

from __future__ import annotations

from trading.domain.enums import ExecutionMode, FamilyId, FamilyResearchStatus
from trading.domain.family_gates import (
    CALENDAR_FAMILIES,
    G1_EXCEEDS_BUDGET_FAMILIES,
    G2_UNPROVEN_FAMILIES,
)

__all__ = [
    "CALENDAR_FAMILIES",
    "family_research_status",
    "is_calendar_family",
    "is_experimental_off_strict_book",
    "refuses_same_expiry_payoff",
]

_EXPERIMENTAL_FAMILIES: frozenset[str] = CALENDAR_FAMILIES

_G2_PROVEN_FAMILIES: frozenset[str] = frozenset(
    family.value
    for family in FamilyId
    if family.value not in G2_UNPROVEN_FAMILIES
    and family.value not in CALENDAR_FAMILIES
)


def is_calendar_family(family_id: str | None) -> bool:
    """Return True when the family is a dual-expiry calendar structure."""
    if family_id is None:
        return False
    return family_id in CALENDAR_FAMILIES


def is_experimental_off_strict_book(family_id: str | None) -> bool:
    """Return True when the family must stay off the strict ₹7L production book."""
    if family_id is None:
        return False
    return family_id in _EXPERIMENTAL_FAMILIES


def refuses_same_expiry_payoff(family_id: FamilyId | str) -> bool:
    """Calendars require dual-expiry settlement analysis, not same-expiry formulas."""
    fam = family_id.value if isinstance(family_id, FamilyId) else str(family_id)
    return fam in CALENDAR_FAMILIES


def family_research_status(
    family_id: str,
    *,
    session_stance: ExecutionMode | None = None,
) -> FamilyResearchStatus:
    """Derive the research/lifecycle label shown to operators."""
    if family_id in _EXPERIMENTAL_FAMILIES:
        return FamilyResearchStatus.EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN
    if family_id in G1_EXCEEDS_BUDGET_FAMILIES:
        return FamilyResearchStatus.IMPLEMENTED_UNIT
    if family_id in G2_UNPROVEN_FAMILIES:
        return FamilyResearchStatus.IMPLEMENTED_UNIT
    if session_stance is ExecutionMode.PAPER:
        return FamilyResearchStatus.PAPER_STANCE_ENABLED
    if family_id in _G2_PROVEN_FAMILIES:
        return FamilyResearchStatus.LIFECYCLE_PROVEN
    return FamilyResearchStatus.IMPLEMENTED_UNIT
