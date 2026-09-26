"""Shared G1/G2 and experimental family gate constants."""

from __future__ import annotations

from collections.abc import Mapping

from trading.domain.enums import EntryProfile, ExecutionMode, FamilyId

__all__ = [
    "CALENDAR_FAMILIES",
    "G1_EXCEEDS_BUDGET_FAMILIES",
    "G2_LEGACY_UNPROVEN_STRATEGY_IDS",
    "G2_UNPROVEN_FAMILIES",
    "assert_family_gate_sets_use_valid_family_ids",
    "effective_family_stances",
]

G1_EXCEEDS_BUDGET_FAMILIES: frozenset[str] = frozenset(
    {
        FamilyId.long_straddle.value,
        FamilyId.long_strangle.value,
    }
)

# Lifecycle-unproven families use real ``FamilyId`` values so stance gates fire.
G2_UNPROVEN_FAMILIES: frozenset[str] = frozenset()

# Legacy strategy ids kept out of the strict book; not ``FamilyId`` members.
G2_LEGACY_UNPROVEN_STRATEGY_IDS: frozenset[str] = frozenset(
    {
        "defined_risk_multileg",
        "iron_condor",
    }
)

CALENDAR_FAMILIES: frozenset[str] = frozenset(
    {
        FamilyId.long_call_calendar.value,
        FamilyId.long_put_calendar.value,
    }
)


def effective_family_stances(
    family_stances: Mapping[str, ExecutionMode],
    *,
    entry_profile: EntryProfile = EntryProfile.STRICT,
    discovery_family_stances: Mapping[str, ExecutionMode] | None = None,
) -> dict[str, ExecutionMode]:
    """Merge discovery profile overrides when ``entry_profile`` is DISCOVERY."""
    merged = dict(family_stances)
    if entry_profile is EntryProfile.DISCOVERY and discovery_family_stances is not None:
        merged.update(discovery_family_stances)
    return merged


def assert_family_gate_sets_use_valid_family_ids() -> None:
    """Fail fast when a gate set drifts from canonical ``FamilyId`` values."""
    valid = {member.value for member in FamilyId}
    for family in G1_EXCEEDS_BUDGET_FAMILIES | G2_UNPROVEN_FAMILIES | CALENDAR_FAMILIES:
        if family not in valid:
            raise AssertionError(f"gate family {family!r} is not a valid FamilyId")
