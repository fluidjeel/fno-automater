"""Observed P1 selection features. Absence is explicit; nothing is invented."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from trading.domain.contracts import FeatureSnapshot, MarketState
from trading.domain.contracts.paper_data import (
    P1RvWindows,
    PaperDataField,
    PaperDataRequirements,
)
from trading.domain.enums import OptionType

__all__ = [
    "IvPoint",
    "ObservedP1Features",
    "blocked_families",
    "observe_exit_depth",
    "observe_p1_features",
    "top_book_size",
]

_ZERO = Decimal(0)
_QUANT = Decimal("0.000001")


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
    term_slope: Decimal | None
    realized_volatility_annualized: Decimal | None
    realized_volatility_ratio: Decimal | None
    depth_top_size: tuple[tuple[str, int], ...]
    present: tuple[PaperDataField, ...]
    absent: tuple[PaperDataField, ...]
    absence_reasons: tuple[tuple[PaperDataField, str], ...]

    def atm_iv(self, expiry: date | None) -> Decimal | None:
        """Observed ATM IV for one expiry, or None when that expiry was not seen."""
        if expiry is None:
            return None
        for item_expiry, value in self.atm_iv_by_expiry:
            if item_expiry == expiry:
                return value
        return None

    def reason_labels(self) -> tuple[str, ...]:
        """Stable audit strings for SetupFeatures; never a filled series."""
        return tuple(
            f"{field.value}:{reason}" for field, reason in self.absence_reasons
        )


def observe_p1_features(
    candidates: Sequence[FeatureSnapshot],
    *,
    requirements: PaperDataRequirements,
    market: MarketState | None = None,
) -> ObservedP1Features:
    """Build P1 features from observed chain/market/depth only."""
    windows = requirements.windows
    points = tuple(_point(item) for item in candidates if _point(item) is not None)
    typed_points = tuple(item for item in points if item is not None)
    surface_spec = requirements.spec_for(PaperDataField.IV_SURFACE)
    min_surface = surface_spec.min_points or 4
    surface_ok = len(typed_points) >= min_surface
    surface_reason = None if surface_ok else "fewer_than_min_points"

    skew = _observed_skew(
        typed_points,
        delta_min=windows.iv_skew.delta_min,
        delta_max=windows.iv_skew.delta_max,
    )
    skew_reason = None if skew is not None else "missing_25d_put_or_call"

    term = _observed_term(typed_points)
    term_spec = requirements.spec_for(PaperDataField.TERM_STRUCTURE)
    min_term = term_spec.min_points or 2
    term_ok = len(term) >= min_term
    term_reason = None if term_ok else "fewer_than_min_expiries"
    term_slope = _term_slope(term) if term_ok else None

    rv_ann, rv_ratio, rv_ok, rv_reason = _observed_rv(
        market, windows.realized_volatility
    )

    greeks_ok = any(
        _has_greeks(item, windows.greeks.require_delta, windows.greeks.require_iv)
        for item in candidates
    )
    greeks_reason = None if greeks_ok else "missing_delta_or_iv"

    depth_sizes = tuple(
        (item.contract.symbol, size)
        for item in candidates
        if (size := top_book_size(item)) is not None
        and size >= windows.depth.min_top_size
    )
    depth_ok = len(depth_sizes) >= windows.depth.min_book_levels
    depth_reason = None if depth_ok else "missing_top_of_book_size"

    present: list[PaperDataField] = []
    absent: list[PaperDataField] = []
    reasons: list[tuple[PaperDataField, str]] = []
    _record(
        present,
        absent,
        reasons,
        field=PaperDataField.IV_SURFACE,
        ok=surface_ok,
        reason=surface_reason,
    )
    _record(
        present,
        absent,
        reasons,
        field=PaperDataField.IV_SKEW,
        ok=skew is not None,
        reason=skew_reason,
    )
    _record(
        present,
        absent,
        reasons,
        field=PaperDataField.TERM_STRUCTURE,
        ok=term_ok,
        reason=term_reason,
    )
    _record(
        present,
        absent,
        reasons,
        field=PaperDataField.REALIZED_VOLATILITY,
        ok=rv_ok,
        reason=rv_reason,
    )
    _record(
        present,
        absent,
        reasons,
        field=PaperDataField.GREEKS,
        ok=greeks_ok,
        reason=greeks_reason,
    )
    _record(
        present,
        absent,
        reasons,
        field=PaperDataField.DEPTH,
        ok=depth_ok,
        reason=depth_reason,
    )
    return ObservedP1Features(
        points=typed_points,
        iv_skew=skew,
        term_atm_iv=term if term_ok else (),
        atm_iv_by_expiry=term,
        term_slope=term_slope,
        realized_volatility_annualized=rv_ann if rv_ok else None,
        realized_volatility_ratio=rv_ratio if rv_ok else None,
        depth_top_size=depth_sizes,
        present=tuple(present),
        absent=tuple(absent),
        absence_reasons=tuple(reasons),
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


def observe_exit_depth(
    snapshot: FeatureSnapshot, requirements: PaperDataRequirements
) -> tuple[bool, str]:
    """Observe top-of-book size on an exit snapshot. Never invents depth.

    Missing size is fail-visible. Protective exits still evaluate; CAS and
    conservative fills stay depth-dependent via existing policy.
    """
    windows = requirements.windows.depth
    if not windows.observe_on_exit:
        return True, "exit_depth_not_configured"
    size = top_book_size(snapshot)
    if size is None:
        return False, "missing_top_of_book_size"
    if size < windows.min_top_size:
        return False, "top_of_book_below_min_size"
    return True, "observed"


def top_book_size(candidate: FeatureSnapshot) -> int | None:
    """Displayed min(bid, ask) size, or None when either side is unobserved."""
    bid_size, ask_size = candidate.market.bid_size, candidate.market.ask_size
    if bid_size is None or ask_size is None:
        return None
    displayed = min(bid_size, ask_size)
    return displayed if displayed > 0 else None


def _record(
    present: list[PaperDataField],
    absent: list[PaperDataField],
    reasons: list[tuple[PaperDataField, str]],
    *,
    field: PaperDataField,
    ok: bool,
    reason: str | None,
) -> None:
    if ok:
        present.append(field)
        return
    absent.append(field)
    reasons.append((field, reason or "p1_not_observed"))


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


def _observed_skew(
    points: Sequence[IvPoint], *, delta_min: Decimal, delta_max: Decimal
) -> Decimal | None:
    """25-delta put IV minus 25-delta call IV when both wings are observed."""
    puts = [
        item.implied_volatility
        for item in points
        if item.option_type is OptionType.PUT
        and _near_delta(item.delta, delta_min, delta_max)
    ]
    calls = [
        item.implied_volatility
        for item in points
        if item.option_type is OptionType.CALL
        and _near_delta(item.delta, delta_min, delta_max)
    ]
    if not puts or not calls:
        return None
    put_iv = sum(puts, _ZERO) / Decimal(len(puts))
    call_iv = sum(calls, _ZERO) / Decimal(len(calls))
    return _q(put_iv - call_iv)


def _near_delta(delta: Decimal | None, minimum: Decimal, maximum: Decimal) -> bool:
    if delta is None:
        return False
    magnitude = abs(delta)
    return minimum <= magnitude <= maximum


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


def _term_slope(term: Sequence[tuple[date, Decimal]]) -> Decimal | None:
    """Front ATM IV / back ATM IV. <1 means the front is cheaper (no fill)."""
    if len(term) < 2:  # noqa: PLR2004 - slope is undefined on a single expiry
        return None
    front, back = term[0][1], term[-1][1]
    if back <= 0:
        return None
    return _q(front / back)


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


def _observed_rv(
    market: MarketState | None, windows: P1RvWindows
) -> tuple[Decimal | None, Decimal | None, bool, str | None]:
    if market is None:
        return None, None, False, "missing_or_unwarmed_history"
    rv_ann = market.realized_volatility_annualized
    rv_ratio = market.realized_volatility_ratio
    if rv_ann is None or rv_ann <= _ZERO:
        return None, None, False, "missing_or_unwarmed_history"
    if market.completed_bar_count < windows.long_window_bars:
        return None, None, False, "fewer_than_long_window_bars"
    return rv_ann, rv_ratio, True, None


def _has_greeks(
    candidate: FeatureSnapshot, require_delta: bool, require_iv: bool
) -> bool:
    derivatives = candidate.derivatives
    if derivatives is None or derivatives.greeks is None:
        return False
    greeks = derivatives.greeks
    if not greeks.converged:
        return False
    if require_delta and greeks.delta is None:
        return False
    return not (require_iv and greeks.implied_volatility is None)


def _q(value: Decimal) -> Decimal:
    return value.quantize(_QUANT)
