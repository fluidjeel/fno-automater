"""Deterministic same-expiry option payoff and kink/slope checker.

Covers §5.2 and §7 of NIFTY_FOUR_MODE_CURSOR_REDESIGN.md and Scenario T03.
Validates bounded loss, kink points, and tail slopes for same-expiry structures,
comparing generic piecewise-linear evaluation against structure-specific formulas.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, unique

from trading.domain.contracts.base import StrictModel
from trading.domain.enums import FamilyId, OptionType, Side
from trading.domain.primitives import Currency, Money, Rounding
from trading.research.registry import refuses_same_expiry_payoff

__all__ = [
    "PayoffLeg",
    "PayoffPoint",
    "PayoffReport",
    "PayoffStatus",
    "evaluate_same_expiry_payoff",
    "formula_bear_call_credit",
    "formula_bear_put_debit",
    "formula_bull_call_debit",
    "formula_bull_put_credit",
    "formula_long_call_butterfly",
    "formula_long_put_butterfly",
    "formula_long_straddle",
    "formula_long_strangle",
    "formula_short_iron_butterfly_defined",
    "formula_short_iron_condor_defined",
]

_VERTICAL_SPREAD_LEG_COUNT = 2
_CONDOR_LEG_COUNT = 4


@unique
class PayoffStatus(StrEnum):
    IMPLEMENTED_UNIT = "IMPLEMENTED_UNIT"
    GEOMETRY_INVALID = "GEOMETRY_INVALID"
    UNBOUNDED_LOSS = "UNBOUNDED_LOSS"
    FORMULA_MISMATCH = "FORMULA_MISMATCH"
    DUAL_EXPIRY_REQUIRED = "DUAL_EXPIRY_REQUIRED"


@dataclass(frozen=True, slots=True)
class PayoffLeg:
    """One leg in a same-expiry option structure."""

    strike: Decimal
    option_type: OptionType
    side: Side
    premium: Decimal
    quantity: int = 1

    def __post_init__(self) -> None:
        if self.strike <= Decimal("0"):
            raise ValueError(f"strike must be positive, got {self.strike}")
        if self.premium < Decimal("0"):
            raise ValueError(f"premium cannot be negative, got {self.premium}")
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")


@dataclass(frozen=True, slots=True)
class PayoffPoint:
    """Payoff evaluated at a specific spot price."""

    spot: Decimal
    pnl_per_contract: Decimal
    total_pnl: Money


class PayoffReport(StrictModel):
    """Result of generic kink/slope payoff analysis and formula cross-check."""

    family_id: FamilyId | str
    is_bounded_loss: bool
    is_bounded_profit: bool
    upper_tail_slope: Decimal
    lower_tail_slope: Decimal
    kink_evaluations: tuple[dict[str, str], ...]
    generic_max_loss: Money
    generic_max_profit: Money | None
    formula_max_loss: Money | None
    formula_max_profit: Money | None
    formula_agrees: bool
    status: PayoffStatus


def _signed_quantity(leg: PayoffLeg) -> Decimal:
    return Decimal(leg.quantity) if leg.side is Side.BUY else Decimal(-leg.quantity)


def _intrinsic(spot: Decimal, strike: Decimal, option_type: OptionType) -> Decimal:
    if option_type is OptionType.CALL:
        return max(spot - strike, Decimal("0"))
    return max(strike - spot, Decimal("0"))


def _evaluate_pnl_at_spot(
    spot: Decimal,
    legs: Sequence[PayoffLeg],
) -> Decimal:
    """Calculate net terminal P&L per structure unit at spot price S."""
    total_pnl = Decimal("0")
    for leg in legs:
        signed_qty = _signed_quantity(leg)
        intrinsic = _intrinsic(spot, leg.strike, leg.option_type)
        cash_flow = -signed_qty * leg.premium
        total_pnl += (signed_qty * intrinsic) + cash_flow
    return total_pnl


def _compute_tail_slopes(legs: Sequence[PayoffLeg]) -> tuple[Decimal, Decimal]:
    """Calculate (upper_tail_slope, lower_tail_slope)."""
    upper_slope = Decimal("0")
    lower_slope = Decimal("0")
    for leg in legs:
        signed_qty = _signed_quantity(leg)
        if leg.option_type is OptionType.CALL:
            upper_slope += signed_qty
        elif leg.option_type is OptionType.PUT:
            lower_slope -= signed_qty
    return upper_slope, lower_slope


def _build_evaluation_spots(strikes: list[Decimal]) -> list[Decimal]:
    critical_spots = [Decimal("0"), *strikes]
    eval_spots = list(set(critical_spots))
    for i in range(len(strikes) - 1):
        mid = (strikes[i] + strikes[i + 1]) / Decimal("2")
        eval_spots.append(mid)
    max_strike = strikes[-1]
    eval_spots.append(max_strike + Decimal("500"))
    return sorted(set(eval_spots))


def _determine_status(
    *,
    is_bounded_loss: bool,
    formula_matched: bool,
    formula_agrees: bool,
) -> PayoffStatus:
    if not is_bounded_loss:
        return PayoffStatus.UNBOUNDED_LOSS
    if formula_matched and not formula_agrees:
        return PayoffStatus.FORMULA_MISMATCH
    return PayoffStatus.IMPLEMENTED_UNIT


def evaluate_same_expiry_payoff(
    legs: Sequence[PayoffLeg],
    *,
    lot_size: int,
    structure_lots: int = 1,
    charges: Money | None = None,
    currency: Currency = Currency.INR,
    family_id: FamilyId | str = "custom",
) -> PayoffReport:
    """Perform generic kink/slope analysis and cross-check with analytical formula."""
    if refuses_same_expiry_payoff(family_id):
        fam = family_id.value if isinstance(family_id, FamilyId) else str(family_id)
        return PayoffReport(
            family_id=fam,
            is_bounded_loss=False,
            is_bounded_profit=False,
            upper_tail_slope=Decimal("0"),
            lower_tail_slope=Decimal("0"),
            kink_evaluations=(),
            generic_max_loss=Money.of("999999999", currency),
            generic_max_profit=None,
            formula_max_loss=None,
            formula_max_profit=None,
            formula_agrees=False,
            status=PayoffStatus.DUAL_EXPIRY_REQUIRED,
        )
    if not legs:
        raise ValueError("payoff evaluation requires at least one leg")
    if lot_size <= 0:
        raise ValueError(f"lot_size must be positive, got {lot_size}")
    if structure_lots <= 0:
        raise ValueError(f"structure_lots must be positive, got {structure_lots}")

    cost_allowance = charges if charges is not None else Money.zero(currency)
    contracts = Decimal(lot_size * structure_lots)

    strikes = sorted({leg.strike for leg in legs})
    critical_spots = [Decimal("0"), *strikes]
    eval_spots = _build_evaluation_spots(strikes)

    upper_slope, lower_slope = _compute_tail_slopes(legs)
    is_bounded_loss = upper_slope >= Decimal("0")
    is_bounded_profit = upper_slope <= Decimal("0")

    pnl_by_spot: dict[Decimal, Decimal] = {}
    kink_records: list[dict[str, str]] = []
    for spot in eval_spots:
        pnl_per_unit = _evaluate_pnl_at_spot(spot, legs)
        total_pnl_amt = (pnl_per_unit * contracts) - cost_allowance.amount
        pnl_by_spot[spot] = total_pnl_amt
        kink_records.append(
            {
                "spot": str(spot),
                "pnl_per_unit": str(pnl_per_unit),
                "total_pnl": str(total_pnl_amt),
            }
        )

    kink_pnls = [pnl_by_spot[s] for s in critical_spots]

    if not is_bounded_loss:
        generic_max_loss = Money.of("999999999", currency)
    else:
        min_pnl = min(kink_pnls)
        generic_max_loss_val = max(-min_pnl, Decimal("0"))
        generic_max_loss = Money.of(str(generic_max_loss_val), currency).quantized(
            Rounding.CEILING
        )

    if not is_bounded_profit:
        generic_max_profit = None
    else:
        max_pnl = max(kink_pnls)
        generic_max_profit_val = max(max_pnl, Decimal("0"))
        generic_max_profit = Money.of(str(generic_max_profit_val), currency).quantized(
            Rounding.FLOOR
        )

    formula_loss, formula_profit, formula_matched = _compute_formula_payoff(
        family_id=family_id,
        legs=legs,
        lot_size=lot_size,
        structure_lots=structure_lots,
        charges=cost_allowance,
        currency=currency,
    )

    formula_agrees = False
    if formula_matched and formula_loss is not None:
        loss_diff = abs(generic_max_loss.amount - formula_loss.amount)
        if not is_bounded_profit or generic_max_profit is None:
            formula_agrees = loss_diff == Decimal("0")
        elif formula_profit is not None:
            profit_diff = abs(generic_max_profit.amount - formula_profit.amount)
            formula_agrees = loss_diff == Decimal("0") and profit_diff == Decimal("0")

    status = _determine_status(
        is_bounded_loss=is_bounded_loss,
        formula_matched=formula_matched,
        formula_agrees=formula_agrees,
    )

    return PayoffReport(
        family_id=family_id,
        is_bounded_loss=is_bounded_loss,
        is_bounded_profit=is_bounded_profit,
        upper_tail_slope=upper_slope,
        lower_tail_slope=lower_slope,
        kink_evaluations=tuple(kink_records),
        generic_max_loss=generic_max_loss,
        generic_max_profit=generic_max_profit,
        formula_max_loss=formula_loss,
        formula_max_profit=formula_profit,
        formula_agrees=formula_agrees,
        status=status,
    )


# ---------------------------------------------------------------------------
# Specific Analytical Formulas
# ---------------------------------------------------------------------------


def formula_bull_call_debit(
    *,
    lower_strike: Decimal,
    higher_strike: Decimal,
    buy_premium: Decimal,
    sell_premium: Decimal,
    lot_size: int,
    structure_lots: int = 1,
    charges: Money | None = None,
    currency: Currency = Currency.INR,
) -> tuple[Money, Money]:
    """Analytical max loss and max profit for Bull Call Debit.

    Buy Call @ K1, Sell Call @ K2 (K1 < K2).
    Net Debit = p_buy - p_sell > 0.
    Max Loss = Net Debit * lot_size * structure_lots + charges.
    Max Profit = (Width - Net Debit) * lot_size * structure_lots - charges.
    """
    if higher_strike <= lower_strike:
        raise ValueError("higher_strike must be strictly greater than lower_strike")
    net_debit = buy_premium - sell_premium
    if net_debit <= Decimal("0"):
        raise ValueError("bull call debit requires net debit > 0")
    width = higher_strike - lower_strike
    if net_debit >= width:
        raise ValueError("net debit must be less than spread width")

    contracts = Decimal(lot_size * structure_lots)
    cost = charges if charges is not None else Money.zero(currency)

    loss_amt = (net_debit * contracts) + cost.amount
    profit_amt = ((width - net_debit) * contracts) - cost.amount

    return (
        Money.of(str(loss_amt), currency).quantized(Rounding.CEILING),
        Money.of(str(profit_amt), currency).quantized(Rounding.FLOOR),
    )


def formula_bear_put_debit(
    *,
    lower_strike: Decimal,
    higher_strike: Decimal,
    buy_premium: Decimal,
    sell_premium: Decimal,
    lot_size: int,
    structure_lots: int = 1,
    charges: Money | None = None,
    currency: Currency = Currency.INR,
) -> tuple[Money, Money]:
    """Analytical max loss and max profit for Bear Put Debit.

    Buy Put @ K2 (higher), Sell Put @ K1 (lower).
    Net Debit = p_buy - p_sell > 0.
    Max Loss = Net Debit * lot_size * structure_lots + charges.
    Max Profit = (Width - Net Debit) * lot_size * structure_lots - charges.
    """
    if higher_strike <= lower_strike:
        raise ValueError("higher_strike must be strictly greater than lower_strike")
    net_debit = buy_premium - sell_premium
    if net_debit <= Decimal("0"):
        raise ValueError("bear put debit requires net debit > 0")
    width = higher_strike - lower_strike
    if net_debit >= width:
        raise ValueError("net debit must be less than spread width")

    contracts = Decimal(lot_size * structure_lots)
    cost = charges if charges is not None else Money.zero(currency)

    loss_amt = (net_debit * contracts) + cost.amount
    profit_amt = ((width - net_debit) * contracts) - cost.amount

    return (
        Money.of(str(loss_amt), currency).quantized(Rounding.CEILING),
        Money.of(str(profit_amt), currency).quantized(Rounding.FLOOR),
    )


def formula_bull_put_credit(
    *,
    lower_strike: Decimal,
    higher_strike: Decimal,
    sell_premium: Decimal,
    buy_premium: Decimal,
    lot_size: int,
    structure_lots: int = 1,
    charges: Money | None = None,
    currency: Currency = Currency.INR,
) -> tuple[Money, Money]:
    """Analytical max loss and max profit for Bull Put Credit.

    Sell Put @ K2 (higher), Buy Put @ K1 (lower).
    Net Credit = p_sell - p_buy > 0.
    Width = K2 - K1.
    Max Loss = (Width - Net Credit) * lot_size * structure_lots + charges.
    Max Profit = Net Credit * lot_size * structure_lots - charges.
    """
    if higher_strike <= lower_strike:
        raise ValueError("higher_strike must be strictly greater than lower_strike")
    net_credit = sell_premium - buy_premium
    if net_credit <= Decimal("0"):
        raise ValueError("bull put credit requires net credit > 0")
    width = higher_strike - lower_strike
    if net_credit >= width:
        raise ValueError("net credit must be less than spread width")

    contracts = Decimal(lot_size * structure_lots)
    cost = charges if charges is not None else Money.zero(currency)

    loss_amt = ((width - net_credit) * contracts) + cost.amount
    profit_amt = (net_credit * contracts) - cost.amount

    return (
        Money.of(str(loss_amt), currency).quantized(Rounding.CEILING),
        Money.of(str(profit_amt), currency).quantized(Rounding.FLOOR),
    )


def formula_bear_call_credit(
    *,
    lower_strike: Decimal,
    higher_strike: Decimal,
    sell_premium: Decimal,
    buy_premium: Decimal,
    lot_size: int,
    structure_lots: int = 1,
    charges: Money | None = None,
    currency: Currency = Currency.INR,
) -> tuple[Money, Money]:
    """Analytical max loss and max profit for Bear Call Credit.

    Sell Call @ K1 (lower), Buy Call @ K2 (higher).
    Net Credit = p_sell - p_buy > 0.
    Width = K2 - K1.
    Max Loss = (Width - Net Credit) * lot_size * structure_lots + charges.
    Max Profit = Net Credit * lot_size * structure_lots - charges.
    """
    if higher_strike <= lower_strike:
        raise ValueError("higher_strike must be strictly greater than lower_strike")
    net_credit = sell_premium - buy_premium
    if net_credit <= Decimal("0"):
        raise ValueError("bear call credit requires net credit > 0")
    width = higher_strike - lower_strike
    if net_credit >= width:
        raise ValueError("net credit must be less than spread width")

    contracts = Decimal(lot_size * structure_lots)
    cost = charges if charges is not None else Money.zero(currency)

    loss_amt = ((width - net_credit) * contracts) + cost.amount
    profit_amt = (net_credit * contracts) - cost.amount

    return (
        Money.of(str(loss_amt), currency).quantized(Rounding.CEILING),
        Money.of(str(profit_amt), currency).quantized(Rounding.FLOOR),
    )


def formula_short_iron_condor_defined(
    *,
    long_put_strike: Decimal,
    short_put_strike: Decimal,
    short_call_strike: Decimal,
    long_call_strike: Decimal,
    long_put_premium: Decimal,
    short_put_premium: Decimal,
    short_call_premium: Decimal,
    long_call_premium: Decimal,
    lot_size: int,
    structure_lots: int = 1,
    charges: Money | None = None,
    currency: Currency = Currency.INR,
) -> tuple[Money, Money]:
    """Analytical max loss and max profit for a short iron condor.

    Loss bound uses the wider wing width minus net credit received.
    """
    if not (long_put_strike < short_put_strike < short_call_strike < long_call_strike):
        raise ValueError("iron condor strikes must satisfy lp < sp < sc < lc")
    put_width = short_put_strike - long_put_strike
    call_width = long_call_strike - short_call_strike
    wider_wing = max(put_width, call_width)
    net_credit = (short_put_premium + short_call_premium) - (
        long_put_premium + long_call_premium
    )
    if net_credit <= Decimal("0"):
        raise ValueError("short iron condor requires positive net credit")
    if net_credit >= wider_wing:
        raise ValueError("net credit must be less than the wider wing width")

    contracts = Decimal(lot_size * structure_lots)
    cost = charges if charges is not None else Money.zero(currency)

    loss_amt = ((wider_wing - net_credit) * contracts) + cost.amount
    profit_amt = (net_credit * contracts) - cost.amount
    return (
        Money.of(str(loss_amt), currency).quantized(Rounding.CEILING),
        Money.of(str(profit_amt), currency).quantized(Rounding.FLOOR),
    )


def formula_long_call_butterfly(
    *,
    low_strike: Decimal,
    mid_strike: Decimal,
    high_strike: Decimal,
    low_premium: Decimal,
    mid_premium: Decimal,
    high_premium: Decimal,
    lot_size: int,
    structure_lots: int = 1,
    charges: Money | None = None,
    currency: Currency = Currency.INR,
) -> tuple[Money, Money]:
    """Analytical max loss and max profit for a long 1:2:1 call butterfly."""
    if not (low_strike < mid_strike < high_strike):
        raise ValueError("call butterfly strikes must satisfy low < mid < high")
    if high_strike - mid_strike != mid_strike - low_strike:
        raise ValueError("call butterfly wings must be symmetric")
    net_debit = low_premium - (Decimal("2") * mid_premium) + high_premium
    if net_debit <= Decimal("0"):
        raise ValueError("long call butterfly requires positive net debit")
    wing = mid_strike - low_strike
    contracts = Decimal(lot_size * structure_lots)
    cost = charges if charges is not None else Money.zero(currency)
    loss_amt = (net_debit * contracts) + cost.amount
    profit_amt = ((wing - net_debit) * contracts) - cost.amount
    return (
        Money.of(str(loss_amt), currency).quantized(Rounding.CEILING),
        Money.of(str(profit_amt), currency).quantized(Rounding.FLOOR),
    )


def formula_long_put_butterfly(
    *,
    low_strike: Decimal,
    mid_strike: Decimal,
    high_strike: Decimal,
    low_premium: Decimal,
    mid_premium: Decimal,
    high_premium: Decimal,
    lot_size: int,
    structure_lots: int = 1,
    charges: Money | None = None,
    currency: Currency = Currency.INR,
) -> tuple[Money, Money]:
    """Analytical max loss and max profit for a long 1:2:1 put butterfly."""
    if not (low_strike < mid_strike < high_strike):
        raise ValueError("put butterfly strikes must satisfy low < mid < high")
    if high_strike - mid_strike != mid_strike - low_strike:
        raise ValueError("put butterfly wings must be symmetric")
    net_debit = low_premium - (Decimal("2") * mid_premium) + high_premium
    if net_debit <= Decimal("0"):
        raise ValueError("long put butterfly requires positive net debit")
    wing = mid_strike - low_strike
    contracts = Decimal(lot_size * structure_lots)
    cost = charges if charges is not None else Money.zero(currency)
    loss_amt = (net_debit * contracts) + cost.amount
    profit_amt = ((wing - net_debit) * contracts) - cost.amount
    return (
        Money.of(str(loss_amt), currency).quantized(Rounding.CEILING),
        Money.of(str(profit_amt), currency).quantized(Rounding.FLOOR),
    )


def formula_short_iron_butterfly_defined(
    *,
    long_put_strike: Decimal,
    short_strike: Decimal,
    long_call_strike: Decimal,
    long_put_premium: Decimal,
    short_put_premium: Decimal,
    short_call_premium: Decimal,
    long_call_premium: Decimal,
    lot_size: int,
    structure_lots: int = 1,
    charges: Money | None = None,
    currency: Currency = Currency.INR,
) -> tuple[Money, Money]:
    """Analytical max loss and max profit for a short iron butterfly."""
    if not (long_put_strike < short_strike < long_call_strike):
        raise ValueError("iron butterfly strikes must satisfy lp < center < lc")
    put_wing = short_strike - long_put_strike
    call_wing = long_call_strike - short_strike
    wider_wing = max(put_wing, call_wing)
    net_credit = (short_put_premium + short_call_premium) - (
        long_put_premium + long_call_premium
    )
    if net_credit <= Decimal("0"):
        raise ValueError("short iron butterfly requires positive net credit")
    if net_credit >= wider_wing:
        raise ValueError("net credit must be less than the wider wing width")
    contracts = Decimal(lot_size * structure_lots)
    cost = charges if charges is not None else Money.zero(currency)
    loss_amt = ((wider_wing - net_credit) * contracts) + cost.amount
    profit_amt = (net_credit * contracts) - cost.amount
    return (
        Money.of(str(loss_amt), currency).quantized(Rounding.CEILING),
        Money.of(str(profit_amt), currency).quantized(Rounding.FLOOR),
    )


def formula_long_straddle(
    *,
    strike: Decimal,
    call_premium: Decimal,
    put_premium: Decimal,
    lot_size: int,
    structure_lots: int = 1,
    charges: Money | None = None,
    currency: Currency = Currency.INR,
) -> tuple[Money, Money]:
    """Analytical max loss for a long straddle (profit is unbounded)."""
    net_debit = call_premium + put_premium
    if net_debit <= Decimal("0"):
        raise ValueError("long straddle requires positive net debit")
    contracts = Decimal(lot_size * structure_lots)
    cost = charges if charges is not None else Money.zero(currency)
    loss_amt = (net_debit * contracts) + cost.amount
    return (
        Money.of(str(loss_amt), currency).quantized(Rounding.CEILING),
        Money.of("0", currency),
    )


def formula_long_strangle(
    *,
    put_strike: Decimal,
    call_strike: Decimal,
    put_premium: Decimal,
    call_premium: Decimal,
    lot_size: int,
    structure_lots: int = 1,
    charges: Money | None = None,
    currency: Currency = Currency.INR,
) -> tuple[Money, Money]:
    """Analytical max loss for a long strangle (profit is unbounded)."""
    if put_strike >= call_strike:
        raise ValueError("strangle put strike must be below call strike")
    net_debit = put_premium + call_premium
    if net_debit <= Decimal("0"):
        raise ValueError("long strangle requires positive net debit")
    contracts = Decimal(lot_size * structure_lots)
    cost = charges if charges is not None else Money.zero(currency)
    loss_amt = (net_debit * contracts) + cost.amount
    return (
        Money.of(str(loss_amt), currency).quantized(Rounding.CEILING),
        Money.of("0", currency),
    )


def _parse_long_butterfly_legs(
    legs: Sequence[PayoffLeg],
) -> tuple[PayoffLeg, PayoffLeg, PayoffLeg] | None:
    if len(legs) != 3:
        return None
    option_types = {leg.option_type for leg in legs}
    if len(option_types) != 1:
        return None
    buys = [leg for leg in legs if leg.side is Side.BUY]
    sells = [leg for leg in legs if leg.side is Side.SELL]
    if len(buys) != 2 or len(sells) != 1:
        return None
    if sells[0].quantity != 2:
        return None
    low_buy = min(buys, key=lambda leg: leg.strike)
    high_buy = max(buys, key=lambda leg: leg.strike)
    mid_sell = sells[0]
    if not (low_buy.strike < mid_sell.strike < high_buy.strike):
        return None
    if high_buy.strike - mid_sell.strike != mid_sell.strike - low_buy.strike:
        return None
    return low_buy, mid_sell, high_buy


def _parse_iron_butterfly_legs(
    legs: Sequence[PayoffLeg],
) -> tuple[PayoffLeg, PayoffLeg, PayoffLeg, PayoffLeg] | None:
    puts = [leg for leg in legs if leg.option_type is OptionType.PUT]
    calls = [leg for leg in legs if leg.option_type is OptionType.CALL]
    if len(puts) != 2 or len(calls) != 2:
        return None
    long_put = next((leg for leg in puts if leg.side is Side.BUY), None)
    short_put = next((leg for leg in puts if leg.side is Side.SELL), None)
    short_call = next((leg for leg in calls if leg.side is Side.SELL), None)
    long_call = next((leg for leg in calls if leg.side is Side.BUY), None)
    if long_put is None or short_put is None or short_call is None or long_call is None:
        return None
    if short_put.strike != short_call.strike:
        return None
    if not (long_put.strike < short_put.strike < long_call.strike):
        return None
    return long_put, short_put, short_call, long_call


def _parse_iron_condor_legs(
    legs: Sequence[PayoffLeg],
) -> tuple[PayoffLeg, PayoffLeg, PayoffLeg, PayoffLeg] | None:
    puts = [leg for leg in legs if leg.option_type is OptionType.PUT]
    calls = [leg for leg in legs if leg.option_type is OptionType.CALL]
    if len(puts) != 2 or len(calls) != 2:
        return None
    long_put = next((leg for leg in puts if leg.side is Side.BUY), None)
    short_put = next((leg for leg in puts if leg.side is Side.SELL), None)
    short_call = next((leg for leg in calls if leg.side is Side.SELL), None)
    long_call = next((leg for leg in calls if leg.side is Side.BUY), None)
    if long_put is None or short_put is None or short_call is None or long_call is None:
        return None
    if not (long_put.strike < short_put.strike < short_call.strike < long_call.strike):
        return None
    return long_put, short_put, short_call, long_call


def _compute_formula_payoff(
    family_id: FamilyId | str,
    legs: Sequence[PayoffLeg],
    *,
    lot_size: int,
    structure_lots: int,
    charges: Money,
    currency: Currency,
) -> tuple[Money | None, Money | None, bool]:
    """Match legs to one of the four verticals and compute analytical formula."""
    fam_str = family_id.value if isinstance(family_id, FamilyId) else str(family_id)

    if (
        fam_str
        in {
            FamilyId.short_iron_condor_defined.value,
            "short_iron_condor_defined",
        }
        and len(legs) == _CONDOR_LEG_COUNT
    ):
        parsed = _parse_iron_condor_legs(legs)
        if parsed is not None:
            long_put, short_put, short_call, long_call = parsed
            loss, profit = formula_short_iron_condor_defined(
                long_put_strike=long_put.strike,
                short_put_strike=short_put.strike,
                short_call_strike=short_call.strike,
                long_call_strike=long_call.strike,
                long_put_premium=long_put.premium,
                short_put_premium=short_put.premium,
                short_call_premium=short_call.premium,
                long_call_premium=long_call.premium,
                lot_size=lot_size,
                structure_lots=structure_lots,
                charges=charges,
                currency=currency,
            )
            return loss, profit, True

    if fam_str in {
        FamilyId.long_call_butterfly.value,
        FamilyId.long_put_butterfly.value,
        "long_call_butterfly",
        "long_put_butterfly",
    }:
        butterfly = _parse_long_butterfly_legs(legs)
        if butterfly is not None:
            low_buy, mid_sell, high_buy = butterfly
            formula = (
                formula_long_call_butterfly
                if low_buy.option_type is OptionType.CALL
                else formula_long_put_butterfly
            )
            loss, profit = formula(
                low_strike=low_buy.strike,
                mid_strike=mid_sell.strike,
                high_strike=high_buy.strike,
                low_premium=low_buy.premium,
                mid_premium=mid_sell.premium,
                high_premium=high_buy.premium,
                lot_size=lot_size,
                structure_lots=structure_lots,
                charges=charges,
                currency=currency,
            )
            return loss, profit, True

    if (
        fam_str
        in {
            FamilyId.short_iron_butterfly_defined.value,
            "short_iron_butterfly_defined",
        }
        and len(legs) == _CONDOR_LEG_COUNT
    ):
        parsed = _parse_iron_butterfly_legs(legs)
        if parsed is not None:
            long_put, short_put, short_call, long_call = parsed
            loss, profit = formula_short_iron_butterfly_defined(
                long_put_strike=long_put.strike,
                short_strike=short_put.strike,
                long_call_strike=long_call.strike,
                long_put_premium=long_put.premium,
                short_put_premium=short_put.premium,
                short_call_premium=short_call.premium,
                long_call_premium=long_call.premium,
                lot_size=lot_size,
                structure_lots=structure_lots,
                charges=charges,
                currency=currency,
            )
            return loss, profit, True

    if fam_str in {FamilyId.long_straddle.value, "long_straddle"} and len(legs) == 2:
        call = next((leg for leg in legs if leg.option_type is OptionType.CALL), None)
        put = next((leg for leg in legs if leg.option_type is OptionType.PUT), None)
        if (
            call is not None
            and put is not None
            and call.side is Side.BUY
            and put.side is Side.BUY
            and call.strike == put.strike
        ):
            loss, profit = formula_long_straddle(
                strike=call.strike,
                call_premium=call.premium,
                put_premium=put.premium,
                lot_size=lot_size,
                structure_lots=structure_lots,
                charges=charges,
                currency=currency,
            )
            return loss, profit, True

    if fam_str in {FamilyId.long_strangle.value, "long_strangle"} and len(legs) == 2:
        call = next((leg for leg in legs if leg.option_type is OptionType.CALL), None)
        put = next((leg for leg in legs if leg.option_type is OptionType.PUT), None)
        if (
            call is not None
            and put is not None
            and call.side is Side.BUY
            and put.side is Side.BUY
            and put.strike < call.strike
        ):
            loss, profit = formula_long_strangle(
                put_strike=put.strike,
                call_strike=call.strike,
                put_premium=put.premium,
                call_premium=call.premium,
                lot_size=lot_size,
                structure_lots=structure_lots,
                charges=charges,
                currency=currency,
            )
            return loss, profit, True

    if len(legs) != _VERTICAL_SPREAD_LEG_COUNT:
        return None, None, False

    leg1, leg2 = legs[0], legs[1]

    if fam_str in {FamilyId.bull_call_debit.value, "bull_call_debit"}:
        buy = leg1 if leg1.side is Side.BUY else leg2
        sell = leg2 if leg1.side is Side.BUY else leg1
        if (
            buy.option_type is OptionType.CALL
            and sell.option_type is OptionType.CALL
            and buy.strike < sell.strike
        ):
            loss, profit = formula_bull_call_debit(
                lower_strike=buy.strike,
                higher_strike=sell.strike,
                buy_premium=buy.premium,
                sell_premium=sell.premium,
                lot_size=lot_size,
                structure_lots=structure_lots,
                charges=charges,
                currency=currency,
            )
            return loss, profit, True

    elif fam_str in {FamilyId.bear_put_debit.value, "bear_put_debit"}:
        buy = leg1 if leg1.side is Side.BUY else leg2
        sell = leg2 if leg1.side is Side.BUY else leg1
        if (
            buy.option_type is OptionType.PUT
            and sell.option_type is OptionType.PUT
            and buy.strike > sell.strike
        ):
            loss, profit = formula_bear_put_debit(
                lower_strike=sell.strike,
                higher_strike=buy.strike,
                buy_premium=buy.premium,
                sell_premium=sell.premium,
                lot_size=lot_size,
                structure_lots=structure_lots,
                charges=charges,
                currency=currency,
            )
            return loss, profit, True

    elif fam_str in {FamilyId.bull_put_credit.value, "bull_put_credit"}:
        sell = leg1 if leg1.side is Side.SELL else leg2
        buy = leg2 if leg1.side is Side.SELL else leg1
        if (
            sell.option_type is OptionType.PUT
            and buy.option_type is OptionType.PUT
            and sell.strike > buy.strike
        ):
            loss, profit = formula_bull_put_credit(
                lower_strike=buy.strike,
                higher_strike=sell.strike,
                sell_premium=sell.premium,
                buy_premium=buy.premium,
                lot_size=lot_size,
                structure_lots=structure_lots,
                charges=charges,
                currency=currency,
            )
            return loss, profit, True

    elif fam_str in {FamilyId.bear_call_credit.value, "bear_call_credit"}:
        sell = leg1 if leg1.side is Side.SELL else leg2
        buy = leg2 if leg1.side is Side.SELL else leg1
        if (
            sell.option_type is OptionType.CALL
            and buy.option_type is OptionType.CALL
            and sell.strike < buy.strike
        ):
            loss, profit = formula_bear_call_credit(
                lower_strike=sell.strike,
                higher_strike=buy.strike,
                sell_premium=sell.premium,
                buy_premium=buy.premium,
                lot_size=lot_size,
                structure_lots=structure_lots,
                charges=charges,
                currency=currency,
            )
            return loss, profit, True

    return None, None, False
