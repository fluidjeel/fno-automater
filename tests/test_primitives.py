"""Unit boundaries for financial value objects.

Covers the parts of SAFETY_INVARIANTS.md that primitives can enforce alone:
explicit units (19), deterministic rounding (21), and no limit bypass through
rounding, which TESTING_AND_RELEASE.md requires as a property test.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from trading.domain.primitives import (
    Currency,
    Lots,
    LotSize,
    Money,
    Percent,
    Price,
    Quantity,
    Rounding,
    TickSize,
    UnitError,
)

INR = Currency.INR
TICK = TickSize.of("0.05")

decimals = st.decimals(
    min_value=Decimal("-1e9"),
    max_value=Decimal("1e9"),
    allow_nan=False,
    allow_infinity=False,
    places=4,
)


class TestMoneyRejectsFloat:
    """No float ever reaches an accounting path."""

    def test_constructor_rejects_float(self) -> None:
        with pytest.raises(UnitError, match="float"):
            Money.of(100.5, INR)  # type: ignore[arg-type]

    def test_multiplication_rejects_float(self) -> None:
        with pytest.raises(UnitError, match="float"):
            Money.of("100", INR) * 1.5  # type: ignore[operator]

    def test_division_rejects_float(self) -> None:
        with pytest.raises(UnitError, match="float"):
            Money.of("100", INR) / 1.5  # type: ignore[operator]

    def test_rejects_nan_and_infinity(self) -> None:
        for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with pytest.raises(UnitError, match="finite"):
                Money(bad, INR)


class TestMoneyCurrencySafety:
    def test_cannot_add_across_currencies(self) -> None:
        with pytest.raises(UnitError, match="convert explicitly"):
            Money.of("1", Currency.INR) + Money.of("1", Currency.USD)

    def test_cannot_compare_across_currencies(self) -> None:
        with pytest.raises(UnitError, match="convert explicitly"):
            _ = Money.of("1", Currency.INR) < Money.of("1", Currency.USD)

    def test_equality_across_currencies_is_false_not_an_error(self) -> None:
        # Dataclass equality compares fields, so INR 1 != USD 1 without raising.
        assert Money.of("1", Currency.INR) != Money.of("1", Currency.USD)


class TestMoneyRounding:
    def test_rounding_is_banker_s_and_deterministic(self) -> None:
        # Half-even: 0.125 -> 0.12 and 0.135 -> 0.14. Half-up would bias upward.
        assert Money.of("0.125", INR).quantized().amount == Decimal("0.12")
        assert Money.of("0.135", INR).quantized().amount == Decimal("0.14")

    def test_directional_rounding_is_explicit(self) -> None:
        value = Money.of("1.111", INR)
        assert value.quantized(Rounding.FLOOR).amount == Decimal("1.11")
        assert value.quantized(Rounding.CEILING).amount == Decimal("1.12")

    def test_floor_is_signed_not_magnitude_based(self) -> None:
        """FLOOR moves toward minus infinity, so it grows a negative magnitude.

        This is why the enum is named for the number line: a "round down" that
        turned -0.001 into -0.01 would quietly enlarge a modelled loss.
        """
        assert Money.of("-1.111", INR).quantized(Rounding.FLOOR).amount == Decimal(
            "-1.12"
        )
        assert Money.of("-1.111", INR).quantized(
            Rounding.TOWARD_ZERO
        ).amount == Decimal("-1.11")

    @given(decimals)
    def test_quantize_is_idempotent(self, raw: Decimal) -> None:
        once = Money(raw, INR).quantized()
        assert once.quantized() == once

    @given(decimals)
    def test_toward_zero_never_increases_magnitude(self, raw: Decimal) -> None:
        """No limit bypass through rounding, at either sign."""
        rounded = Money(raw, INR).quantized(Rounding.TOWARD_ZERO).amount
        assert abs(rounded) <= abs(raw)

    @given(decimals)
    def test_nearest_rounding_moves_by_less_than_one_minor_unit(
        self, raw: Decimal
    ) -> None:
        """A limit check cannot be bypassed by more than half a paisa."""
        rounded = Money(raw, INR).quantized().amount
        assert abs(rounded - raw) <= Decimal("0.005")

    def test_division_does_not_lose_exactness_silently(self) -> None:
        # 100 / 3 keeps 34 significant digits rather than collapsing to 2 places.
        third = Money.of("100", INR) / 3
        assert third.amount != Decimal("33.33")
        assert third.quantized().amount == Decimal("33.33")


class TestPriceTickGrid:
    def test_off_tick_price_is_rejected(self) -> None:
        with pytest.raises(UnitError, match="not a multiple of tick"):
            Price(Decimal("100.03"), TICK)

    def test_snap_places_value_on_the_grid(self) -> None:
        assert Price.snap("100.03", TICK).value == Decimal("100.05")
        assert Price.snap("100.03", TICK, Rounding.FLOOR).value == Decimal("100.00")
        assert Price.snap("100.02", TICK, Rounding.CEILING).value == Decimal("100.05")

    def test_negative_price_is_rejected(self) -> None:
        with pytest.raises(UnitError, match="non-negative"):
            Price(Decimal("-0.05"), TICK)

    def test_cannot_compare_prices_across_tick_grids(self) -> None:
        with pytest.raises(UnitError, match="different tick grids"):
            _ = Price(Decimal("100"), TICK) > Price(Decimal("100"), TickSize.of("0.10"))

    def test_tick_distance_is_integral(self) -> None:
        """Stop distance is measured in ticks, so the result must be exact."""
        entry = Price(Decimal("100.00"), TICK)
        stop = Price(Decimal("99.50"), TICK)
        assert stop.ticks_from(entry) == -10

    def test_tick_size_must_be_positive(self) -> None:
        with pytest.raises(UnitError, match="positive"):
            TickSize.of("0")

    def test_notional_uses_contracts_not_lots(self) -> None:
        price = Price(Decimal("100.00"), TICK)
        assert price.notional(Quantity(50), INR) == Money.of("5000.00", INR)


class TestQuantityAndLotsDoNotMix:
    def test_adding_lots_to_quantity_is_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            _ = Quantity(50) + Lots(1)  # type: ignore[operator]

    def test_adding_quantity_to_lots_is_a_type_error(self) -> None:
        with pytest.raises(TypeError):
            _ = Lots(1) + Quantity(50)  # type: ignore[operator]

    def test_conversion_requires_an_explicit_lot_size(self) -> None:
        lot_size = LotSize(75)
        assert Lots(2).to_quantity(lot_size) == Quantity(150)
        assert Quantity(150).to_lots(lot_size) == Lots(2)

    def test_partial_lot_is_rejected(self) -> None:
        with pytest.raises(UnitError, match="not a whole multiple of lot size"):
            Quantity(100).to_lots(LotSize(75))

    def test_short_quantity_is_representable(self) -> None:
        assert Quantity(-150).is_short
        assert Quantity(-150).to_lots(LotSize(75)) == Lots(-2)

    def test_bool_is_not_an_acceptable_count(self) -> None:
        # bool is an int subclass; accepting it would let True mean one contract.
        with pytest.raises(UnitError, match="must be an int"):
            Quantity(True)

    def test_lot_size_must_be_positive(self) -> None:
        with pytest.raises(UnitError, match="positive"):
            LotSize(0)


class TestPercentUnits:
    def test_constructors_are_unambiguous(self) -> None:
        assert Percent.from_percent("2").fraction == Decimal("0.02")
        assert Percent.from_bps("200").fraction == Decimal("0.02")
        assert Percent.from_fraction("0.02").fraction == Decimal("0.02")

    def test_round_trips_through_every_representation(self) -> None:
        two_percent = Percent.from_percent("2")
        assert two_percent.as_percent == Decimal("2.00")
        assert two_percent.as_bps == Decimal("200.00")

    def test_applying_to_money_preserves_currency(self) -> None:
        cap = Percent.from_percent("2").of(Money.of("700000", INR))
        assert cap == Money.of("14000.00", INR)

    @given(st.integers(min_value=0, max_value=10_000))
    def test_bps_and_percent_agree(self, bps: int) -> None:
        assert Percent.from_bps(bps) == Percent.from_percent(Decimal(bps) / 100)
