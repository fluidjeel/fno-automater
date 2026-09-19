"""Observed P1 selection features. Absence is explicit; nothing is invented."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from trading.domain.contracts import FeatureSnapshot, MarketState
from trading.domain.contracts.paper_data import PaperDataField, PaperDataRequirements
from trading.domain.enums import OptionType

__all__ = ["IvPoint", "ObservedP1Features", "blocked_families", "observe_p1_features"]

_ZERO = Decimal(0)
_SKEW_DELTA_MIN = Decimal("0.20")
_SKEW_DELTA_MAX = Decimal("0.30")


@dataclass(frozen=True, slots=True)
class IvPoint:
    """One observed chain IV. No interpolated or filled strike."""

    expiry: date
    strike: Decimal
    option_type: OptionType
    implied_volatility: Decimal
    delta: Decimal | None
    underlying_price: Decimal | None


@dataclass(frozen=True, slots=True)
class ObservedP1Features:
    """Honest P1 observation over one candidate universe."""

    points: tuple[IvPoint, ...]
    iv_skew: Decimal | None
    term_atm_iv: tuple[tuple[date, Decimal], ...]
    atm_iv_by_expiry: tuple[tuple[date, Decimal], ...]
    realized_volatility_annualized: Decimal | None
    realized_volatility_ratio: Decimal | None
    present: tuple[PaperDataField, ...]
    absent: tuple[PaperDataField, ...]

    def atm_iv(self, expiry: date | None) -> Decimal | None:
        """Observed ATM IV for one expiry, or None when that expiry was not seen."""
        if expiry is None:
            return None
        for item_expiry, value in self.atm_iv_by_expiry:
            if item_expiry == expiry:
                return value
        return None


def observe_p1_features(
    candidates: Sequence[FeatureSnapshot],
    *,
    requirements: PaperDataRequirements,
    market: MarketState | None = None,
) -> ObservedP1Features:
    """Build P1 features from observed chain/market state only."""
    points = tuple(_point(item) for item in candidates if _point(item) is not None)
    typed_points = tuple(item for item in points if item is not None)
    surface_spec = requirements.spec_for(PaperDataField.IV_SURFACE)
    min_surface = surface_spec.min_points or 4
    surface_ok = len(typed_points) >= min_surface

    skew = _observed_skew(typed_points)
    term = _observed_term(typed_points)
    term_spec = requirements.spec_for(PaperDataField.TERM_STRUCTURE)
    min_term = term_spec.min_points or 2
    term_ok = len(term) >= min_term

    rv_ann = None if market is None else market.realized_volatility_annualized
    rv_ratio = None if market is None else market.realized_volatility_ratio
    rv_ok = rv_ann is not None and rv_ann > _ZERO

    greeks_ok = any(_has_greeks(item) for item in candidates)
    depth_ok = any(_has_depth(item) for item in candidates)

    present: list[PaperDataField] = []
    absent: list[PaperDataField] = []
    _record(present, absent, PaperDataField.IV_SURFACE, surface_ok)
    _record(present, absent, PaperDataField.IV_SKEW, skew is not None)
    _record(present, absent, PaperDataField.TERM_STRUCTURE, term_ok)
    _record(present, absent, PaperDataField.REALIZED_VOLATILITY, rv_ok)
    _record(present, absent, PaperDataField.GREEKS, greeks_ok)
    _record(present, absent, PaperDataField.DEPTH, depth_ok)
    return ObservedP1Features(
        points=typed_points,
        iv_skew=skew,
        term_atm_iv=term if term_ok else (),
        atm_iv_by_expiry=term,
        realized_volatility_annualized=rv_ann if rv_ok else None,
        realized_volatility_ratio=rv_ratio if rv_ok else None,
        present=tuple(present),
        absent=tuple(absent),
    )


def blocked_families(
    observed: ObservedP1Features, requirements: PaperDataRequirements
) -> frozenset[str]:
    """Families that policy refuses when a required P1 series is absent."""
    blocked: set[str] = set()
    absent = set(observed.absent)
    for spec in requirements.p1:
        if spec.field in absent:
            blocked.update(spec.block_families)
    return frozenset(blocked)


def _record(
    present: list[PaperDataField],
    absent: list[PaperDataField],
    field: PaperDataField,
    ok: bool,
) -> None:
    if ok:
        present.append(field)
    else:
        absent.append(field)


def _point(candidate: FeatureSnapshot) -> IvPoint | None:
    derivatives = candidate.derivatives
    contract = candidate.contract
    if (
        derivatives is None
        or derivatives.greeks is None
        or not derivatives.greeks.converged
        or derivatives.greeks.implied_volatility is None
        or contract.expiry is None
        or contract.strike is None
        or contract.option_type is None
    ):
        return None
    iv = derivatives.greeks.implied_volatility
    if iv <= 0:
        return None
    underlying = (
        None
        if derivatives.underlying_price is None
        else derivatives.underlying_price.value
    )
    return IvPoint(
        expiry=contract.expiry,
        strike=contract.strike,
        option_type=contract.option_type,
        implied_volatility=iv,
        delta=derivatives.greeks.delta,
        underlying_price=underlying,
    )


def _observed_skew(points: Sequence[IvPoint]) -> Decimal | None:
    """25-delta put IV minus 25-delta call IV when both wings are observed."""
    puts = [
        item.implied_volatility
        for item in points
        if item.option_type is OptionType.PUT and _near_25d(item.delta)
    ]
    calls = [
        item.implied_volatility
        for item in points
        if item.option_type is OptionType.CALL and _near_25d(item.delta)
    ]
    if not puts or not calls:
        return None
    put_iv = sum(puts, _ZERO) / Decimal(len(puts))
    call_iv = sum(calls, _ZERO) / Decimal(len(calls))
    return _q(put_iv - call_iv)


def _near_25d(delta: Decimal | None) -> bool:
    if delta is None:
        return False
    magnitude = abs(delta)
    return _SKEW_DELTA_MIN <= magnitude <= _SKEW_DELTA_MAX


def _observed_term(points: Sequence[IvPoint]) -> tuple[tuple[date, Decimal], ...]:
    by_expiry: dict[date, list[IvPoint]] = {}
    for point in points:
        by_expiry.setdefault(point.expiry, []).append(point)
    rows: list[tuple[date, Decimal]] = []
    for expiry, group in by_expiry.items():
        atm_iv = _atm_iv(group)
        if atm_iv is not None:
            rows.append((expiry, atm_iv))
    rows.sort(key=lambda item: item[0])
    return tuple(rows)


def _atm_iv(points: Sequence[IvPoint]) -> Decimal | None:
    with_spot = [item for item in points if item.underlying_price is not None]
    if not with_spot:
        return None
    nearest = min(
        abs(item.strike - (item.underlying_price or _ZERO)) for item in with_spot
    )
    closest = [
        item
        for item in with_spot
        if abs(item.strike - (item.underlying_price or _ZERO)) == nearest
    ]
    ivs = sorted(item.implied_volatility for item in closest)
    if len(ivs) % 2 == 1:
        return ivs[len(ivs) // 2]
    return (ivs[len(ivs) // 2 - 1] + ivs[len(ivs) // 2]) / 2


def _has_greeks(candidate: FeatureSnapshot) -> bool:
    derivatives = candidate.derivatives
    if derivatives is None or derivatives.greeks is None:
        return False
    greeks = derivatives.greeks
    return (
        greeks.converged
        and greeks.delta is not None
        and greeks.implied_volatility is not None
    )


def _has_depth(candidate: FeatureSnapshot) -> bool:
    bid_size, ask_size = candidate.market.bid_size, candidate.market.ask_size
    return (
        bid_size is not None and ask_size is not None and bid_size > 0 and ask_size > 0
    )


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))
