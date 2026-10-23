"""Validate strategy family_id against legs and producer slot."""

from __future__ import annotations

from trading.domain.contracts.intent import TradeIntent
from trading.domain.enums import FamilyId, InstrumentKind, OptionType

__all__ = [
    "family_id_from_legs",
    "validate_intent_family_alignment",
]


def family_id_from_legs(intent: TradeIntent) -> FamilyId | None:
    """Derive long_call/long_put from the first option leg, if present."""
    for leg in intent.legs:
        contract = leg.contract
        if contract.instrument_kind is not InstrumentKind.OPTION:
            continue
        option_type = contract.option_type
        if option_type is OptionType.CALL:
            return FamilyId.long_call
        if option_type is OptionType.PUT:
            return FamilyId.long_put
    return None


def validate_intent_family_alignment(
    intent: TradeIntent,
    *,
    producer_family_id: FamilyId | None,
) -> str | None:
    """Return a rejection detail when family_id is inconsistent; else None."""
    declared = intent.family_id
    if declared is None:
        return "trade intent missing family_id"
    option_legs = [
        leg
        for leg in intent.legs
        if leg.contract.instrument_kind is InstrumentKind.OPTION
    ]
    if len(option_legs) == 1:
        from_legs = family_id_from_legs(intent)
        if from_legs is not None and declared != from_legs.value:
            return (
                f"family_id {declared!r} does not match option leg "
                f"({from_legs.value!r})"
            )
    if producer_family_id is not None and declared != producer_family_id.value:
        return (
            f"family_id {declared!r} does not match producer slot "
            f"({producer_family_id.value!r})"
        )
    return None
