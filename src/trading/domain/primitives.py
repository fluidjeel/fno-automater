"""Financial value objects. No float ever reaches an accounting path.

Units are encoded in types rather than convention: Money carries a currency,
Price carries a tick size, Quantity counts contracts, Lots counts lots, and
Percent stores a fraction. Arithmetic that crosses units is a TypeError, not a
silently wrong number.
"""

from __future__ import annotations

from dataclasses import dataclass

# Invariant 21 requires reproducible decisions, so nearest-rounding is always
# banker's rounding. Half-up would bias sizing upward on every tie.
from decimal import (
    ROUND_CEILING,
    ROUND_DOWN,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Decimal,
    InvalidOperation,
    localcontext,
)
from enum import StrEnum
from typing import Final, Self

__all__ = [
    "Bps",
    "Currency",
    "LotSize",
    "Lots",
    "Money",
    "Percent",
    "Price",
    "Quantity",
    "Rounding",
    "TickSize",
    "UnitError",
]


class UnitError(ValueError):
    """Raised when a value violates its unit, precision or sign contract."""


class Rounding(StrEnum):
    """Explicit rounding direction.

    Named for the number line, not for magnitude, because "round down" is
    ambiguous once amounts can be negative: the floor of -0.001 is -0.01, which
    *increases* the magnitude of a loss. Use TOWARD_ZERO when the intent is to
    stay conservative regardless of sign.
    """

    HALF_EVEN = "HALF_EVEN"
    FLOOR = "FLOOR"
    CEILING = "CEILING"
    TOWARD_ZERO = "TOWARD_ZERO"


class Currency(StrEnum):
    """Currencies the platform accounts in."""

    INR = "INR"
    USD = "USD"

    @property
    def minor_units(self) -> int:
        """Decimal places in the smallest denomination."""
        return 2


_ROUNDING_MODES: Final[dict[Rounding, str]] = {
    Rounding.HALF_EVEN: ROUND_HALF_EVEN,
    Rounding.FLOOR: ROUND_FLOOR,
    Rounding.CEILING: ROUND_CEILING,
    Rounding.TOWARD_ZERO: ROUND_DOWN,
}
_PRECISION: Final = 34


def _as_decimal(value: Decimal | int | str, field: str) -> Decimal:
    """Coerce to Decimal, rejecting float outright."""
    if isinstance(value, float):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise UnitError(
            f"{field} received a float; binary floats cannot represent decimal "
            "money exactly. Pass a Decimal, int or str."
        )
    if isinstance(value, Decimal):
        decimal_value = value
    else:
        try:
            decimal_value = Decimal(value)
        except InvalidOperation as exc:
            raise UnitError(f"{field} is not a valid decimal: {value!r}") from exc
    if not decimal_value.is_finite():
        raise UnitError(f"{field} must be finite, got {decimal_value}")
    return decimal_value


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """An exact monetary amount in a single currency."""

    amount: Decimal
    currency: Currency

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", _as_decimal(self.amount, "Money.amount"))
        if not isinstance(self.currency, Currency):
            raise UnitError(f"Money.currency must be a Currency, got {self.currency!r}")

    @classmethod
    def of(cls, amount: Decimal | int | str, currency: Currency) -> Self:
        """Construct from any exact decimal representation."""
        return cls(_as_decimal(amount, "amount"), currency)

    @classmethod
    def zero(cls, currency: Currency) -> Self:
        return cls(Decimal(0), currency)

    def quantized(self, rounding: Rounding = Rounding.HALF_EVEN) -> Money:
        """Round to the currency's minor units. Call before settling or comparing."""
        exponent = Decimal(1).scaleb(-self.currency.minor_units)
        with localcontext() as ctx:
            ctx.prec = _PRECISION
            return Money(
                self.amount.quantize(exponent, rounding=_ROUNDING_MODES[rounding]),
                self.currency,
            )

    def _require_same_currency(self, other: Money) -> None:
        if self.currency is not other.currency:
            raise UnitError(
                f"cannot combine {self.currency} and {other.currency}; "
                "convert explicitly with a dated FX rate"
            )

    def __add__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        self._require_same_currency(other)
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.currency)

    def __mul__(self, factor: Decimal | int) -> Money:
        if isinstance(factor, float):
            raise UnitError("cannot scale Money by a float; use Decimal")
        if not isinstance(factor, Decimal | int):
            return NotImplemented
        return Money(self.amount * _as_decimal(factor, "factor"), self.currency)

    __rmul__ = __mul__

    def __truediv__(self, divisor: Decimal | int) -> Money:
        if isinstance(divisor, float):
            raise UnitError("cannot divide Money by a float; use Decimal")
        if not isinstance(divisor, Decimal | int):
            return NotImplemented
        decimal_divisor = _as_decimal(divisor, "divisor")
        if decimal_divisor == 0:
            raise UnitError("division by zero")
        with localcontext() as ctx:
            ctx.prec = _PRECISION
            return Money(self.amount / decimal_divisor, self.currency)

    def __lt__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._require_same_currency(other)
        return self.amount >= other.amount

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    @property
    def is_negative(self) -> bool:
        return self.amount < 0

    def __str__(self) -> str:
        return f"{self.currency} {self.amount}"


@dataclass(frozen=True, slots=True)
class TickSize:
    """Minimum price increment for a contract. Comes from reference data."""

    value: Decimal

    def __post_init__(self) -> None:
        value = _as_decimal(self.value, "TickSize.value")
        if value <= 0:
            raise UnitError(f"TickSize must be positive, got {value}")
        object.__setattr__(self, "value", value)

    @classmethod
    def of(cls, value: Decimal | int | str) -> Self:
        return cls(_as_decimal(value, "value"))


@dataclass(frozen=True, slots=True, order=False)
class Price:
    """A tradable price, guaranteed to sit exactly on its contract's tick grid."""

    value: Decimal
    tick: TickSize

    def __post_init__(self) -> None:
        value = _as_decimal(self.value, "Price.value")
        if value < 0:
            raise UnitError(f"Price must be non-negative, got {value}")
        if not isinstance(self.tick, TickSize):
            raise UnitError(f"Price.tick must be a TickSize, got {self.tick!r}")
        if value % self.tick.value != 0:
            raise UnitError(
                f"price {value} is not a multiple of tick {self.tick.value}; "
                "an off-tick price is rejected by the exchange"
            )
        object.__setattr__(self, "value", value)

    @classmethod
    def snap(
        cls,
        value: Decimal | int | str,
        tick: TickSize,
        rounding: Rounding = Rounding.HALF_EVEN,
    ) -> Self:
        """Build a Price by snapping a raw value onto the tick grid."""
        raw = _as_decimal(value, "value")
        if raw < 0:
            raise UnitError(f"Price must be non-negative, got {raw}")
        with localcontext() as ctx:
            ctx.prec = _PRECISION
            ticks = (raw / tick.value).quantize(
                Decimal(1), rounding=_ROUNDING_MODES[rounding]
            )
            return cls(ticks * tick.value, tick)

    def _require_same_tick(self, other: Price) -> None:
        if self.tick != other.tick:
            raise UnitError(
                f"cannot compare prices on different tick grids "
                f"({self.tick.value} vs {other.tick.value})"
            )

    def __lt__(self, other: Price) -> bool:
        self._require_same_tick(other)
        return self.value < other.value

    def __le__(self, other: Price) -> bool:
        self._require_same_tick(other)
        return self.value <= other.value

    def __gt__(self, other: Price) -> bool:
        self._require_same_tick(other)
        return self.value > other.value

    def __ge__(self, other: Price) -> bool:
        self._require_same_tick(other)
        return self.value >= other.value

    def ticks_from(self, other: Price) -> int:
        """Signed distance in ticks. Stop-distance maths lives in tick space."""
        self._require_same_tick(other)
        return int((self.value - other.value) / self.tick.value)

    def notional(self, quantity: Quantity, currency: Currency) -> Money:
        """Price times contracts. Multipliers beyond lot size are Layer 2's job."""
        return Money(self.value * quantity.contracts, currency)

    def __str__(self) -> str:
        return str(self.value)


@dataclass(frozen=True, slots=True)
class LotSize:
    """Contracts per lot for one instrument. Reference data, never a constant."""

    contracts_per_lot: int

    def __post_init__(self) -> None:
        _require_int(self.contracts_per_lot, "LotSize.contracts_per_lot")
        if self.contracts_per_lot <= 0:
            raise UnitError(f"LotSize must be positive, got {self.contracts_per_lot}")


def _require_int(value: int, field: str) -> None:
    """Exact int only: bool is an int subclass, so True must not mean one."""
    if type(value) is not int:
        raise UnitError(f"{field} must be an int, got {type(value).__name__}")


@dataclass(frozen=True, slots=True, order=True)
class Quantity:
    """A signed count of contracts. Negative is short."""

    contracts: int

    def __post_init__(self) -> None:
        _require_int(self.contracts, "Quantity.contracts")

    @classmethod
    def zero(cls) -> Self:
        return cls(0)

    def to_lots(self, lot_size: LotSize) -> Lots:
        """Convert to lots, rejecting a quantity that is not a whole lot."""
        if self.contracts % lot_size.contracts_per_lot != 0:
            raise UnitError(
                f"{self.contracts} contracts is not a whole multiple of lot size "
                f"{lot_size.contracts_per_lot}; exchanges reject partial lots"
            )
        return Lots(self.contracts // lot_size.contracts_per_lot)

    def __add__(self, other: Quantity) -> Quantity:
        if not isinstance(other, Quantity):
            return NotImplemented
        return Quantity(self.contracts + other.contracts)

    def __sub__(self, other: Quantity) -> Quantity:
        if not isinstance(other, Quantity):
            return NotImplemented
        return Quantity(self.contracts - other.contracts)

    def __neg__(self) -> Quantity:
        return Quantity(-self.contracts)

    def __abs__(self) -> Quantity:
        return Quantity(abs(self.contracts))

    @property
    def is_flat(self) -> bool:
        return self.contracts == 0

    @property
    def is_short(self) -> bool:
        return self.contracts < 0


@dataclass(frozen=True, slots=True, order=True)
class Lots:
    """A signed count of lots. Distinct from Quantity so the two cannot mix."""

    count: int

    def __post_init__(self) -> None:
        _require_int(self.count, "Lots.count")

    @classmethod
    def zero(cls) -> Self:
        return cls(0)

    def to_quantity(self, lot_size: LotSize) -> Quantity:
        return Quantity(self.count * lot_size.contracts_per_lot)

    def __add__(self, other: Lots) -> Lots:
        if not isinstance(other, Lots):
            return NotImplemented
        return Lots(self.count + other.count)

    def __sub__(self, other: Lots) -> Lots:
        if not isinstance(other, Lots):
            return NotImplemented
        return Lots(self.count - other.count)

    def __neg__(self) -> Lots:
        return Lots(-self.count)

    def __abs__(self) -> Lots:
        return Lots(abs(self.count))


@dataclass(frozen=True, slots=True, order=True)
class Percent:
    """A proportion stored as a fraction. 2 percent is Percent(Decimal("0.02"))."""

    fraction: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "fraction", _as_decimal(self.fraction, "Percent.fraction")
        )

    @classmethod
    def from_fraction(cls, fraction: Decimal | int | str) -> Self:
        return cls(_as_decimal(fraction, "fraction"))

    @classmethod
    def from_percent(cls, percent: Decimal | int | str) -> Self:
        with localcontext() as ctx:
            ctx.prec = _PRECISION
            return cls(_as_decimal(percent, "percent") / Decimal(100))

    @classmethod
    def from_bps(cls, bps: Decimal | int | str) -> Self:
        with localcontext() as ctx:
            ctx.prec = _PRECISION
            return cls(_as_decimal(bps, "bps") / Decimal(10_000))

    @property
    def as_percent(self) -> Decimal:
        return self.fraction * Decimal(100)

    @property
    def as_bps(self) -> Decimal:
        return self.fraction * Decimal(10_000)

    def of(self, money: Money) -> Money:
        """Apply this proportion to an amount."""
        return Money(money.amount * self.fraction, money.currency)

    def __str__(self) -> str:
        return f"{self.as_percent}%"


# Basis points are a presentation of Percent, not a second unit. The alias keeps
# call sites readable without creating a type that could diverge.
Bps = Percent
