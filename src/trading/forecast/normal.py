"""Standard normal functions in Decimal."""

from __future__ import annotations

from decimal import Decimal

__all__ = ["norm_cdf", "norm_pdf", "norm_ppf"]

_ZERO = Decimal(0)
_ONE = Decimal(1)
_HALF = Decimal("0.5")
_TWO = Decimal(2)
_INV_SQRT_2PI = Decimal("0.3989422804014326779399460599")

# Abramowitz and Stegun 26.2.17; absolute error below 7.5e-8.
_P = Decimal("0.2316419")
_B = (
    Decimal("0.319381530"),
    Decimal("-0.356563782"),
    Decimal("1.781477937"),
    Decimal("-1.821255978"),
    Decimal("1.330274429"),
)

# Acklam's inverse-normal rational approximation; relative error below 1.2e-9.
_A = (
    Decimal("-39.69683028665376"),
    Decimal("220.9460984245205"),
    Decimal("-275.9285104469687"),
    Decimal("138.3577518672690"),
    Decimal("-30.66479806614716"),
    Decimal("2.506628277459239"),
)
_BB = (
    Decimal("-54.47609879822406"),
    Decimal("161.5858368580409"),
    Decimal("-155.6989798598866"),
    Decimal("66.80131188771972"),
    Decimal("-13.28068155288572"),
)
_C = (
    Decimal("-0.007784894002430293"),
    Decimal("-0.3223964580411365"),
    Decimal("-2.400758277161838"),
    Decimal("-2.549732539343734"),
    Decimal("4.374664141464968"),
    Decimal("2.938163982698783"),
)
_D = (
    Decimal("0.007784695709041462"),
    Decimal("0.3224671290700398"),
    Decimal("2.445134137142996"),
    Decimal("3.754408661907416"),
)
_P_LOW = Decimal("0.02425")
_TAIL_Z = Decimal(12)


def norm_pdf(x: Decimal) -> Decimal:
    """Standard normal density."""
    if abs(x) > _TAIL_Z:
        return _ZERO
    return _INV_SQRT_2PI * (-(x * x) / _TWO).exp()


def norm_cdf(x: Decimal) -> Decimal:
    """Standard normal distribution function."""
    if x > _TAIL_Z:
        return _ONE
    if x < -_TAIL_Z:
        return _ZERO
    magnitude = abs(x)
    t = _ONE / (_ONE + _P * magnitude)
    poly = _ZERO
    for coefficient in reversed(_B):
        poly = (poly + coefficient) * t
    upper_tail = norm_pdf(magnitude) * poly
    return _ONE - upper_tail if x >= 0 else upper_tail


def norm_ppf(p: Decimal) -> Decimal:
    """Inverse standard normal distribution function on the open interval (0, 1)."""
    if p <= 0 or p >= 1:
        raise ValueError(f"probability must be inside (0, 1), got {p}")
    if p < _P_LOW:
        q = (-_TWO * p.ln()).sqrt()
        return _tail(q)
    if p > _ONE - _P_LOW:
        q = (-_TWO * (_ONE - p).ln()).sqrt()
        return -_tail(q)
    q = p - _HALF
    r = q * q
    numerator = _ZERO
    for coefficient in _A:
        numerator = numerator * r + coefficient
    denominator = _ZERO
    for coefficient in _BB:
        denominator = denominator * r + coefficient
    denominator = denominator * r + _ONE
    return numerator * q / denominator


def _tail(q: Decimal) -> Decimal:
    numerator = _ZERO
    for coefficient in _C:
        numerator = numerator * q + coefficient
    denominator = _ZERO
    for coefficient in _D:
        denominator = denominator * q + coefficient
    denominator = denominator * q + _ONE
    return numerator / denominator
