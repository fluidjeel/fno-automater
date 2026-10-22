"""Mode 2 family_id must match option legs and producer slot."""

from __future__ import annotations

from decimal import Decimal

import tests.factories as f
from trading.domain.contracts.intent import IntentLeg, TradeIntent
from trading.domain.enums import FamilyId, ModeId, OptionType, Side
from trading.trade.intent_family import (
    family_id_from_legs,
    validate_intent_family_alignment,
)


def _intent(
    *,
    family_id: str,
    option_type: OptionType,
) -> TradeIntent:
    return f.intent(
        mode_id=ModeId.M2_DIRECTIONAL,
        family_id=family_id,
        legs=(
            IntentLeg(
                leg_id="leg-1",
                contract=f.option_contract(
                    symbol="NIFTY26OCT25000CE"
                    if option_type is OptionType.CALL
                    else "NIFTY26OCT25000PE",
                    option_type=option_type,
                    strike=Decimal("25000"),
                ),
                side=Side.BUY,
                ratio=1,
            ),
        ),
    )


def test_family_from_legs_call() -> None:
    intent = _intent(family_id=FamilyId.long_call.value, option_type=OptionType.CALL)
    assert family_id_from_legs(intent) is FamilyId.long_call


def test_validate_rejects_producer_slot_mismatch() -> None:
    intent = _intent(family_id=FamilyId.long_put.value, option_type=OptionType.PUT)
    detail = validate_intent_family_alignment(
        intent,
        producer_family_id=FamilyId.long_call,
    )
    assert detail is not None
    assert "producer slot" in detail


def test_validate_accepts_aligned_put_slot() -> None:
    intent = _intent(family_id=FamilyId.long_put.value, option_type=OptionType.PUT)
    assert (
        validate_intent_family_alignment(
            intent,
            producer_family_id=FamilyId.long_put,
        )
        is None
    )


def test_multi_leg_family_not_derived_from_first_leg() -> None:
    """Spreads/straddles are not long_call/long_put; must not be rejected."""
    legs = tuple(
        IntentLeg(
            leg_id=f"leg-{idx}",
            contract=f.option_contract(
                symbol=f"NIFTY26OCT{strike}PE",
                option_type=OptionType.PUT,
                strike=Decimal(strike),
            ),
            side=side,
            ratio=1,
        )
        for idx, (strike, side) in enumerate(
            (("22500", Side.BUY), ("22800", Side.SELL))
        )
    )
    intent = f.intent(
        mode_id=ModeId.M3_TACTICAL_POSITIONAL,
        family_id=FamilyId.bull_put_credit.value,
        legs=legs,
    )
    assert family_id_from_legs(intent) is None
    assert (
        validate_intent_family_alignment(
            intent, producer_family_id=FamilyId.bull_put_credit
        )
        is None
    )
