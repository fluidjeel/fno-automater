"""Family leg shapes, forecast-driven strike choice and structure risk levels."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from trading.domain.contracts import FeatureSnapshot
from trading.domain.contracts.forecast import ForecastLeg
from trading.domain.enums import FamilyId, OptionType, Side
from trading.forecast.distribution import HorizonDistribution
from trading.forecast.inputs import annual_vol
from trading.forecast.normal import norm_ppf

__all__ = [
    "FAMILY_DIRECTION",
    "StrikeSuggestion",
    "legs_for_family",
    "max_loss_per_unit",
    "structure_delta",
    "structure_friction",
    "structure_invalidation",
    "suggest_vertical",
]

_ZERO = Decimal(0)
_ONE = Decimal(1)
_TWO = Decimal(2)
_DAYS_PER_YEAR = Decimal(365)

FAMILY_DIRECTION: dict[FamilyId, int] = {
    FamilyId.long_call: 1,
    FamilyId.long_put: -1,
    FamilyId.bull_call_debit: 1,
    FamilyId.bear_put_debit: -1,
    FamilyId.bull_put_credit: 1,
    FamilyId.bear_call_credit: -1,
}
_PAIR_FAMILIES = frozenset(
    {
        FamilyId.bull_call_debit,
        FamilyId.bear_put_debit,
        FamilyId.bull_put_credit,
        FamilyId.bear_call_credit,
    }
)
_ALL_LONG = frozenset(
    {
        FamilyId.long_call,
        FamilyId.long_put,
        FamilyId.long_straddle,
        FamilyId.long_strangle,
    }
)
_IRON = frozenset(
    {FamilyId.short_iron_condor_defined, FamilyId.short_iron_butterfly_defined}
)
_BUTTERFLY = frozenset({FamilyId.long_call_butterfly, FamilyId.long_put_butterfly})


def _mid(snapshot: FeatureSnapshot) -> Decimal | None:
    bid, ask, last = snapshot.market.bid, snapshot.market.ask, snapshot.market.last
    if bid is not None and ask is not None and ask.value >= bid.value > 0:
        return (bid.value + ask.value) / _TWO
    return None if last is None else last.value


def _leg(snapshot: FeatureSnapshot, side: Side, ratio: int = 1) -> ForecastLeg | None:
    contract, derivatives = snapshot.contract, snapshot.derivatives
    mid = _mid(snapshot)
    if (
        contract.option_type is None
        or contract.strike is None
        or derivatives is None
        or mid is None
    ):
        return None
    iv = None
    if derivatives.greeks is not None and derivatives.greeks.converged:
        iv = annual_vol(derivatives.greeks.implied_volatility)
    return ForecastLeg(
        symbol=contract.symbol,
        option_type=contract.option_type,
        strike=contract.strike,
        side=side,
        ratio=ratio,
        expiry_years=Decimal(derivatives.days_to_expiry) / _DAYS_PER_YEAR,
        mid=mid,
        implied_volatility=iv,
    )


def legs_for_family(
    family: FamilyId, candidates: Sequence[FeatureSnapshot]
) -> tuple[ForecastLeg, ...] | None:
    """Signed legs in the family's canonical shape; None when it cannot be built."""
    if not candidates:
        return None
    if family in _ALL_LONG:
        legs = [_leg(item, Side.BUY) for item in candidates]
    elif family in _PAIR_FAMILIES:
        if len(candidates) != 2:  # noqa: PLR2004 - binders emit (long, short)
            return None
        legs = [_leg(candidates[0], Side.BUY), _leg(candidates[1], Side.SELL)]
    elif family in _IRON:
        legs = _iron_legs(candidates)
    elif family in _BUTTERFLY:
        legs = _butterfly_legs(candidates)
    else:
        return None
    if not legs or any(item is None for item in legs):
        return None
    return tuple(item for item in legs if item is not None)


def _strike(snapshot: FeatureSnapshot) -> Decimal:
    return snapshot.contract.strike or _ZERO


def _iron_legs(candidates: Sequence[FeatureSnapshot]) -> list[ForecastLeg | None]:
    """Long the outer wing of each side, short the inner strike."""
    puts = sorted(
        (c for c in candidates if c.contract.option_type is OptionType.PUT), key=_strike
    )
    calls = sorted(
        (c for c in candidates if c.contract.option_type is OptionType.CALL),
        key=_strike,
    )
    if len(puts) != 2 or len(calls) != 2:  # noqa: PLR2004 - four wings
        return []
    return [
        _leg(puts[0], Side.BUY),
        _leg(puts[1], Side.SELL),
        _leg(calls[0], Side.SELL),
        _leg(calls[1], Side.BUY),
    ]


def _butterfly_legs(candidates: Sequence[FeatureSnapshot]) -> list[ForecastLeg | None]:
    """Long both wings, short two of the body."""
    ordered = sorted(candidates, key=_strike)
    strikes = sorted({_strike(item) for item in ordered})
    if len(strikes) != 3:  # noqa: PLR2004 - lower, body, upper
        return []
    lower = next(item for item in ordered if _strike(item) == strikes[0])
    body = next(item for item in ordered if _strike(item) == strikes[1])
    upper = next(item for item in ordered if _strike(item) == strikes[2])
    return [_leg(lower, Side.BUY), _leg(body, Side.SELL, 2), _leg(upper, Side.BUY)]


def structure_friction(
    candidates: Sequence[FeatureSnapshot],
    legs: Sequence[ForecastLeg],
    *,
    charges_per_leg_unit: Decimal,
) -> Decimal:
    """Round-trip friction per structure unit: full quoted spread plus charges."""
    by_symbol = {item.contract.symbol: item for item in candidates}
    total = _ZERO
    for leg in legs:
        snapshot = by_symbol.get(leg.symbol)
        spread = _ZERO
        if snapshot is not None:
            bid, ask = snapshot.market.bid, snapshot.market.ask
            if bid is not None and ask is not None and ask.value >= bid.value:
                spread = ask.value - bid.value
        total += Decimal(leg.ratio) * (spread + charges_per_leg_unit)
    return total


def _intrinsic(leg: ForecastLeg, spot: Decimal) -> Decimal:
    if leg.option_type is OptionType.CALL:
        return max(spot - leg.strike, _ZERO)
    return max(leg.strike - spot, _ZERO)


def max_loss_per_unit(legs: Sequence[ForecastLeg], cost: Decimal) -> Decimal:
    """Worst expiry loss, checked at every strike and far beyond both wings."""
    strikes = sorted({leg.strike for leg in legs})
    probes = [strikes[0] / _TWO, *strikes, strikes[-1] * _TWO]
    worst = min(
        sum(
            (
                (_ONE if leg.side is Side.BUY else -_ONE)
                * Decimal(leg.ratio)
                * _intrinsic(leg, probe)
                for leg in legs
            ),
            _ZERO,
        )
        for probe in probes
    )
    return max(cost - worst, _ZERO)


def structure_delta(
    legs: Sequence[ForecastLeg], candidates: Sequence[FeatureSnapshot]
) -> Decimal | None:
    """Net delta per structure unit; None when any leg lacks a delta."""
    by_symbol = {item.contract.symbol: item for item in candidates}
    total = _ZERO
    for leg in legs:
        snapshot = by_symbol.get(leg.symbol)
        greeks = (
            None
            if snapshot is None or snapshot.derivatives is None
            else snapshot.derivatives.greeks
        )
        if greeks is None or greeks.delta is None:
            return None
        sign = _ONE if leg.side is Side.BUY else -_ONE
        total += sign * Decimal(leg.ratio) * greeks.delta
    return total


def structure_invalidation(
    family: FamilyId, legs: Sequence[ForecastLeg], *, buffer_fraction: Decimal
) -> tuple[Decimal | None, Decimal | None]:
    """NIFTY levels just inside a short strike, or at a butterfly's wings."""
    short_puts = [
        leg.strike
        for leg in legs
        if leg.side is Side.SELL and leg.option_type is OptionType.PUT
    ]
    short_calls = [
        leg.strike
        for leg in legs
        if leg.side is Side.SELL and leg.option_type is OptionType.CALL
    ]
    if family in _BUTTERFLY:
        strikes = sorted(leg.strike for leg in legs)
        return strikes[0], strikes[-1]
    if family in _IRON or family in {
        FamilyId.bull_put_credit,
        FamilyId.bear_call_credit,
    }:
        below = None if not short_puts else max(short_puts) * (_ONE + buffer_fraction)
        above = None if not short_calls else min(short_calls) * (_ONE - buffer_fraction)
        return below, above
    return None, None


@dataclass(frozen=True, slots=True)
class StrikeSuggestion:
    """A vertical chosen from the forecast distribution, in binder order."""

    long_leg: FeatureSnapshot
    short_leg: FeatureSnapshot
    short_level: Decimal


def suggest_vertical(  # noqa: PLR0911 - one early exit per family shape
    family: FamilyId,
    chain: Sequence[FeatureSnapshot],
    *,
    spot: Decimal,
    distribution: HorizonDistribution,
    expiry_days: int,
    credit_short_quantile: Decimal,
    debit_target_quantile: Decimal,
    credit_width_points: Decimal,
    tradable: Callable[[FeatureSnapshot], bool],
) -> StrikeSuggestion | None:
    """Place strikes on the forecast distribution instead of fixed delta bands.

    Credit: short strike where the forecast gives ``credit_short_quantile`` odds
    of never finishing beyond it. Debit: long at the money, short at the
    forecast target quantile.
    """
    option_type = (
        OptionType.CALL
        if family in {FamilyId.bull_call_debit, FamilyId.bear_call_credit}
        else OptionType.PUT
    )
    pool = sorted(
        (
            item
            for item in chain
            if item.contract.option_type is option_type
            and item.contract.strike is not None
            and item.derivatives is not None
            and item.derivatives.days_to_expiry == expiry_days
            and tradable(item)
        ),
        key=_strike,
    )
    if len(pool) < 2:  # noqa: PLR2004 - a vertical needs two strikes
        return None

    def level(q: Decimal) -> Decimal:
        return spot * (distribution.drift + distribution.sigma * norm_ppf(q)).exp()

    def nearest(target: Decimal) -> FeatureSnapshot:
        return min(pool, key=lambda item: abs(_strike(item) - target))

    if family is FamilyId.bull_call_debit:
        long_leg, target = nearest(spot), level(debit_target_quantile)
        shorts = [item for item in pool if _strike(item) > _strike(long_leg)]
        if not shorts:
            return None
        short = min(shorts, key=lambda item: abs(_strike(item) - target))
        return StrikeSuggestion(long_leg=long_leg, short_leg=short, short_level=target)
    if family is FamilyId.bear_put_debit:
        long_leg, target = nearest(spot), level(_ONE - debit_target_quantile)
        shorts = [item for item in pool if _strike(item) < _strike(long_leg)]
        if not shorts:
            return None
        short = min(shorts, key=lambda item: abs(_strike(item) - target))
        return StrikeSuggestion(long_leg=long_leg, short_leg=short, short_level=target)
    if family is FamilyId.bull_put_credit:
        target = level(_ONE - credit_short_quantile)
        shorts = [item for item in pool if _strike(item) <= target]
        if not shorts:
            return None
        short = shorts[-1]
        longs = [
            item
            for item in pool
            if _strike(item) <= _strike(short) - credit_width_points
        ]
        if not longs:
            return None
        return StrikeSuggestion(long_leg=longs[-1], short_leg=short, short_level=target)
    if family is FamilyId.bear_call_credit:
        target = level(credit_short_quantile)
        shorts = [item for item in pool if _strike(item) >= target]
        if not shorts:
            return None
        short = shorts[0]
        longs = [
            item
            for item in pool
            if _strike(item) >= _strike(short) + credit_width_points
        ]
        if not longs:
            return None
        return StrikeSuggestion(long_leg=longs[0], short_leg=short, short_level=target)
    return None
