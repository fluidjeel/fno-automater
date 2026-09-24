"""G2 lifecycle-proven families eligible for review ROLL/SWITCH close paths."""

from __future__ import annotations

from trading.domain.enums import FamilyId

__all__ = ["G2_ROLL_SWITCH_FAMILIES", "family_supports_roll_switch"]

G2_ROLL_SWITCH_FAMILIES: frozenset[str] = frozenset(
    {
        FamilyId.long_call.value,
        FamilyId.long_put.value,
        FamilyId.bull_call_debit.value,
        FamilyId.bear_put_debit.value,
        FamilyId.short_iron_condor_defined.value,
        FamilyId.short_iron_butterfly_defined.value,
        FamilyId.long_call_butterfly.value,
        FamilyId.long_put_butterfly.value,
    }
)


def family_supports_roll_switch(family_id: str | None) -> bool:
    """Return True when the family has proven G2 close and reopen plans."""
    if family_id is None:
        return False
    return family_id in G2_ROLL_SWITCH_FAMILIES
