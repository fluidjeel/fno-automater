"""Horizon log-return distributions and structure valuation under them.

A forecast is a normal log-return distribution over the mode horizon. The
option market's view is the risk-neutral distribution implied by ATM IV. Both
price the same structure on the same grid, so ``forecast value - market cost``
is the edge and ``p_profit`` versus ``p_profit_implied`` is comparable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from trading.domain.contracts.forecast import ForecastLeg
from trading.domain.enums import OptionType, Side
from trading.forecast.normal import norm_cdf, norm_pdf, norm_ppf

__all__ = [
    "HorizonDistribution",
    "StructurePricing",
    "black_price",
    "distribution_from_direction",
    "implied_distribution",
    "price_structure",
    "structure_cost",
    "structure_value",
]

_ZERO = Decimal(0)
_ONE = Decimal(1)
_HALF = Decimal("0.5")
_P_FLOOR = Decimal("0.01")
_P_CEIL = Decimal("0.99")
_PRECISION = 20
_Z_STEP = Decimal("0.2")
_Z_GRID = tuple(Decimal(i) * _Z_STEP for i in range(-30, 31))


def _weights() -> tuple[Decimal, ...]:
    raw = [norm_pdf(z) for z in _Z_GRID]
    total = sum(raw, _ZERO)
    return tuple(item / total for item in raw)


_Z_WEIGHTS = _weights()


@dataclass(frozen=True, slots=True)
class HorizonDistribution:
    """Normal log return over one horizon: mean ``drift``, stdev ``sigma``."""

    drift: Decimal
    sigma: Decimal

    def __post_init__(self) -> None:
        if self.sigma <= 0:
            raise ValueError("horizon sigma must be positive")

    def prob_above(self, log_level: Decimal) -> Decimal:
        """P(log return > level)."""
        return _ONE - norm_cdf((log_level - self.drift) / self.sigma)

    def prob_below(self, log_level: Decimal) -> Decimal:
        """P(log return < level)."""
        return norm_cdf((log_level - self.drift) / self.sigma)

    def prob_between(self, low: Decimal, high: Decimal) -> Decimal:
        """P(low < log return < high)."""
        if high <= low:
            return _ZERO
        return self.prob_below(high) - self.prob_below(low)


def implied_distribution(iv_annual: Decimal, years: Decimal) -> HorizonDistribution:
    """Risk-neutral lognormal (zero rate) implied by an annualized IV."""
    if iv_annual <= 0 or years <= 0:
        raise ValueError("implied distribution needs positive IV and horizon")
    sigma = iv_annual * years.sqrt()
    return HorizonDistribution(drift=-(sigma * sigma) / 2, sigma=sigma)


def distribution_from_direction(p_up: Decimal, sigma: Decimal) -> HorizonDistribution:
    """The normal whose P(return > 0) equals ``p_up`` at the forecast sigma."""
    clamped = min(max(p_up, _P_FLOOR), _P_CEIL)
    drift = _ZERO if clamped == _HALF else sigma * norm_ppf(clamped)
    return HorizonDistribution(drift=drift, sigma=sigma)


def black_price(
    option_type: OptionType,
    spot: Decimal,
    strike: Decimal,
    vol: Decimal,
    years: Decimal,
) -> Decimal:
    """Zero-rate Black-Scholes price; intrinsic when time or vol is exhausted."""
    intrinsic = (
        max(spot - strike, _ZERO)
        if option_type is OptionType.CALL
        else max(strike - spot, _ZERO)
    )
    if years <= 0 or vol <= 0 or spot <= 0:
        return intrinsic
    total_vol = vol * years.sqrt()
    d1 = ((spot / strike).ln() + total_vol * total_vol / 2) / total_vol
    d2 = d1 - total_vol
    if option_type is OptionType.CALL:
        return spot * norm_cdf(d1) - strike * norm_cdf(d2)
    return strike * norm_cdf(-d2) - spot * norm_cdf(-d1)


def _sign(side: Side) -> Decimal:
    return _ONE if side is Side.BUY else -_ONE


def structure_cost(legs: Sequence[ForecastLeg]) -> Decimal:
    """Net debit per unit at mid; negative for a net credit."""
    return sum(
        (_sign(leg.side) * Decimal(leg.ratio) * leg.mid for leg in legs),
        _ZERO,
    )


def structure_value(
    legs: Sequence[ForecastLeg],
    spot: Decimal,
    *,
    elapsed_years: Decimal,
    fallback_vol: Decimal,
) -> Decimal:
    """Mark the structure at ``spot`` after ``elapsed_years`` with frozen leg IV."""
    total = _ZERO
    for leg in legs:
        remaining = max(leg.expiry_years - elapsed_years, _ZERO)
        vol = leg.implied_volatility or fallback_vol
        price = black_price(leg.option_type, spot, leg.strike, vol, remaining)
        total += _sign(leg.side) * Decimal(leg.ratio) * price
    return total


@dataclass(frozen=True, slots=True)
class StructurePricing:
    """Expected value and profit probability of one structure at the horizon."""

    expected_value: Decimal
    p_profit: Decimal


def price_structure(
    legs: Sequence[ForecastLeg],
    *,
    spot: Decimal,
    distribution: HorizonDistribution,
    horizon_years: Decimal,
    cost: Decimal,
    cost_per_unit: Decimal,
    fallback_vol: Decimal,
) -> StructurePricing:
    """Integrate the structure mark over the horizon distribution."""
    elapsed = min(horizon_years, max((leg.expiry_years for leg in legs), default=_ZERO))
    expected = _ZERO
    p_profit = _ZERO
    with localcontext() as ctx:
        ctx.prec = _PRECISION
        for z, weight in zip(_Z_GRID, _Z_WEIGHTS, strict=True):
            terminal = spot * (distribution.drift + distribution.sigma * z).exp()
            value = structure_value(
                legs,
                terminal,
                elapsed_years=elapsed,
                fallback_vol=fallback_vol,
            )
            expected += weight * value
            if value - cost - cost_per_unit > 0:
                p_profit += weight
    return StructurePricing(
        expected_value=+expected,
        p_profit=min(max(+p_profit, _ZERO), _ONE),
    )
