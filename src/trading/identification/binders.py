"""Pure option-chain binders for single-leg and debit-spread strategies."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from trading.domain.contracts import CandidateBinding, FeatureSnapshot, MarketState
from trading.domain.contracts.identification import SetupFeatures, StructureKind
from trading.domain.contracts.paper_data import PaperDataField
from trading.domain.enums import InstrumentKind, OptionType, ReasonCode
from trading.identification.config import IdentificationPolicy
from trading.identification.p1_features import ObservedP1Features, top_book_size

__all__ = ["BoundCandidates", "bind_debit_spread", "bind_long_option"]

_ZERO = Decimal(0)
_ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class BoundCandidates:
    binding: CandidateBinding
    candidates: tuple[FeatureSnapshot, ...]
    setup_features: SetupFeatures | None


def bind_long_option(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    p1: ObservedP1Features | None = None,
) -> BoundCandidates:
    option_type = _direction_type(market)
    eligible = [
        item
        for item in candidates
        if option_type is not None
        and item.contract.option_type is option_type
        and _common_reason(item, policy) is None
        and _long_delta_ok(item, policy)
        and _dte_ok(item, policy)
    ]
    ranked = sorted(
        (
            (_candidate_score(item, candidates, policy, p1=p1), item)
            for item in eligible
        ),
        key=lambda pair: (-pair[0], pair[1].contract.symbol),
    )
    rejected = tuple(
        sorted(item.contract.symbol for item in candidates if item not in eligible)
    )
    if not ranked:
        return BoundCandidates(
            binding=CandidateBinding(
                strategy_id="positional_long_option",
                binding_version=policy.binding_version,
                selected_symbols=(),
                score=_ZERO,
                eligible=False,
                reason_codes=(_dominant_reason(candidates, policy),),
                rejected_symbols=rejected,
            ),
            candidates=(),
            setup_features=None,
        )
    score, selected = ranked[0]
    binding = CandidateBinding(
        strategy_id="positional_long_option",
        binding_version=policy.binding_version,
        selected_symbols=(selected.contract.symbol,),
        score=score,
        eligible=True,
        rejected_symbols=rejected,
    )
    return BoundCandidates(
        binding=binding,
        candidates=(selected,),
        setup_features=_setup(
            market,
            selected,
            structure=StructureKind.LONG_OPTION,
            score=score,
            policy=policy,
            rejected=rejected,
            p1=p1,
        ),
    )


def bind_debit_spread(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    p1: ObservedP1Features | None = None,
) -> BoundCandidates:
    option_type = _direction_type(market)
    legs = [
        item
        for item in candidates
        if option_type is not None
        and item.contract.option_type is option_type
        and _common_reason(item, policy) is None
        and _dte_ok(item, policy)
    ]
    longs = [item for item in legs if _long_delta_ok(item, policy)]
    shorts = [item for item in legs if _short_delta_ok(item, policy)]
    pairs: list[tuple[Decimal, FeatureSnapshot, FeatureSnapshot]] = []
    for long_leg in longs:
        for short_leg in shorts:
            if not _valid_debit_pair(
                long_leg,
                short_leg,
                cast(OptionType, option_type),
                policy,
            ):
                continue
            long_score = _candidate_score(
                long_leg,
                candidates,
                policy,
                delta_range=(
                    policy.contracts.long_delta_min,
                    policy.contracts.long_delta_max,
                ),
                p1=p1,
                role="long",
            )
            short_score = _candidate_score(
                short_leg,
                candidates,
                policy,
                delta_range=(
                    policy.contracts.short_delta_min,
                    policy.contracts.short_delta_max,
                ),
                p1=p1,
                role="short",
            )
            score = (long_score + short_score) / 2
            pairs.append((_q(score), long_leg, short_leg))
    pairs.sort(
        key=lambda row: (
            -row[0],
            row[1].contract.symbol,
            row[2].contract.symbol,
        )
    )
    if not pairs:
        rejected = tuple(sorted(item.contract.symbol for item in candidates))
        return BoundCandidates(
            binding=CandidateBinding(
                strategy_id="debit_spread",
                binding_version=policy.binding_version,
                selected_symbols=(),
                score=_ZERO,
                eligible=False,
                reason_codes=(_dominant_reason(candidates, policy),),
                rejected_symbols=rejected,
            ),
            candidates=(),
            setup_features=None,
        )
    score, long_leg, short_leg = pairs[0]
    selected_symbols = {long_leg.contract.symbol, short_leg.contract.symbol}
    rejected = tuple(
        sorted(
            item.contract.symbol
            for item in candidates
            if item.contract.symbol not in selected_symbols
        )
    )
    binding = CandidateBinding(
        strategy_id="debit_spread",
        binding_version=policy.binding_version,
        selected_symbols=(long_leg.contract.symbol, short_leg.contract.symbol),
        score=score,
        eligible=True,
        rejected_symbols=rejected,
    )
    return BoundCandidates(
        binding=binding,
        candidates=(long_leg, short_leg),
        setup_features=_setup(
            market,
            long_leg,
            structure=StructureKind.DEBIT_SPREAD,
            score=score,
            policy=policy,
            rejected=rejected,
            p1=p1,
        ),
    )


def _common_reason(  # noqa: PLR0911 - explicit fail-closed gate precedence
    candidate: FeatureSnapshot, policy: IdentificationPolicy
) -> ReasonCode | None:
    derivatives = candidate.derivatives
    if (
        candidate.contract.instrument_kind is not InstrumentKind.OPTION
        or derivatives is None
    ):
        return ReasonCode.INSTRUMENT_UNKNOWN
    if not candidate.permits_new_exposure:
        return ReasonCode.DATA_INVALID
    if candidate.features.get("lot_size", _ZERO) <= 0:
        return ReasonCode.INSTRUMENT_UNKNOWN
    if candidate.features.get("top_of_book_observed", _ZERO) != 1:
        return ReasonCode.PRICE_UNAVAILABLE
    if (
        derivatives.open_interest is None
        or derivatives.open_interest < policy.contracts.min_open_interest
    ):
        return ReasonCode.DEPTH_INSUFFICIENT
    greeks = derivatives.greeks
    if (
        greeks is None
        or not greeks.converged
        or greeks.delta is None
        or greeks.implied_volatility is None
    ):
        return ReasonCode.DATA_GAP
    spread = _spread_fraction(candidate)
    if spread is None:
        return ReasonCode.PRICE_UNAVAILABLE
    if spread > policy.contracts.max_spread_fraction:
        return ReasonCode.SPREAD_TOO_WIDE
    return None


def _dte_ok(candidate: FeatureSnapshot, policy: IdentificationPolicy) -> bool:
    derivatives = candidate.derivatives
    if derivatives is None:
        return False
    dte = derivatives.days_to_expiry
    return (
        policy.contracts.weekly_dte_min <= dte <= policy.contracts.weekly_dte_max
        or policy.contracts.monthly_dte_min <= dte <= policy.contracts.monthly_dte_max
    )


def _long_delta_ok(candidate: FeatureSnapshot, policy: IdentificationPolicy) -> bool:
    delta = _abs_delta(candidate)
    return (
        delta is not None
        and policy.contracts.long_delta_min <= delta <= policy.contracts.long_delta_max
    )


def _short_delta_ok(candidate: FeatureSnapshot, policy: IdentificationPolicy) -> bool:
    delta = _abs_delta(candidate)
    return (
        delta is not None
        and policy.contracts.short_delta_min
        <= delta
        <= policy.contracts.short_delta_max
    )


def _valid_debit_pair(  # noqa: PLR0911 - invalid payoff conditions fail closed
    long_leg: FeatureSnapshot,
    short_leg: FeatureSnapshot,
    option_type: OptionType,
    policy: IdentificationPolicy,
) -> bool:
    if long_leg.contract.expiry != short_leg.contract.expiry:
        return False
    long_strike, short_strike = long_leg.contract.strike, short_leg.contract.strike
    if long_strike is None or short_strike is None or long_strike == short_strike:
        return False
    if option_type is OptionType.CALL and long_strike >= short_strike:
        return False
    if option_type is OptionType.PUT and long_strike <= short_strike:
        return False
    long_ask, short_bid = long_leg.market.ask, short_leg.market.bid
    if long_ask is None or short_bid is None:
        return False
    lot = long_leg.features.get("lot_size")
    if lot is None or lot <= 0:
        return False
    cost_points = policy.contracts.estimated_round_trip_cost_per_lot / lot
    debit = long_ask.value - short_bid.value + cost_points
    width = abs(long_strike - short_strike)
    if debit <= 0 or debit >= width:
        return False
    reward_risk = (width - debit) / debit
    return reward_risk >= policy.contracts.min_reward_risk


def _candidate_score(
    candidate: FeatureSnapshot,
    universe: tuple[FeatureSnapshot, ...],
    policy: IdentificationPolicy,
    *,
    delta_range: tuple[Decimal, Decimal] | None = None,
    p1: ObservedP1Features | None = None,
    role: str = "long",
) -> Decimal:
    derivatives = candidate.derivatives
    if derivatives is None:
        return _ZERO
    oi_values = [
        item.derivatives.open_interest
        for item in universe
        if item.derivatives is not None and item.derivatives.open_interest is not None
    ]
    volume_values = [
        item.market.volume for item in universe if item.market.volume is not None
    ]
    oi_score = _ratio(derivatives.open_interest, max(oi_values, default=0))
    volume_score = _ratio(candidate.market.volume, max(volume_values, default=0))
    liquidity = (oi_score + volume_score) / 2
    delta = _abs_delta(candidate) or _ZERO
    minimum_delta, maximum_delta = delta_range or (
        policy.contracts.long_delta_min,
        policy.contracts.long_delta_max,
    )
    delta_mid = (minimum_delta + maximum_delta) / 2
    delta_half = (maximum_delta - minimum_delta) / 2
    delta_score = (
        _ONE
        if delta_half <= 0
        else max(_ZERO, _ONE - abs(delta - delta_mid) / delta_half)
    )
    dte = Decimal(derivatives.days_to_expiry)
    target = Decimal(
        8 if derivatives.days_to_expiry <= policy.contracts.weekly_dte_max else 28
    )
    dte_score = max(_ZERO, _ONE - abs(dte - target) / target)
    spread = _spread_fraction(candidate) or policy.contracts.max_spread_fraction
    spread_score = max(_ZERO, _ONE - spread / policy.contracts.max_spread_fraction)
    base = (
        Decimal("0.35") * liquidity
        + Decimal("0.25") * delta_score
        + Decimal("0.20") * dte_score
        + Decimal("0.20") * spread_score
    )
    extra = _p1_score(candidate, universe, p1, role=role)
    if extra is None:
        return _q(base)
    return _q(Decimal("0.80") * base + Decimal("0.20") * extra)


def _p1_score(
    candidate: FeatureSnapshot,
    universe: tuple[FeatureSnapshot, ...],
    p1: ObservedP1Features | None,
    *,
    role: str,
) -> Decimal | None:
    """Observed-only ranking tilt. Missing P1 series are skipped, never filled."""
    if p1 is None:
        return None
    parts = [
        score
        for score in (
            _p1_iv_score(candidate, p1, role=role),
            _p1_skew_score(candidate, p1),
            _p1_term_score(candidate, p1),
            _p1_rv_score(candidate, p1, role=role),
            _p1_depth_score(candidate, universe, p1),
            _p1_greeks_score(candidate, universe, p1),
        )
        if score is not None
    ]
    if not parts:
        return None
    return sum(parts, _ZERO) / Decimal(len(parts))


def _p1_iv_score(
    candidate: FeatureSnapshot, p1: ObservedP1Features, *, role: str
) -> Decimal | None:
    if PaperDataField.IV_SURFACE not in p1.present:
        return None
    iv = _implied_vol(candidate)
    atm = p1.atm_iv(candidate.contract.expiry)
    if iv is None or atm is None or atm <= 0:
        return None
    relative = iv / atm
    if role == "short":
        return _clamp(_ONE - abs(relative - Decimal("1.10")), _ZERO, _ONE)
    return _clamp(Decimal(2) - relative, _ZERO, _ONE)


def _p1_skew_score(
    candidate: FeatureSnapshot, p1: ObservedP1Features
) -> Decimal | None:
    if PaperDataField.IV_SKEW not in p1.present or p1.iv_skew is None:
        return None
    option_type = candidate.contract.option_type
    if option_type is OptionType.CALL:
        return _ONE if p1.iv_skew > 0 else Decimal("0.5")
    if option_type is OptionType.PUT:
        return _ONE if p1.iv_skew < 0 else Decimal("0.5")
    return None


def _p1_term_score(
    candidate: FeatureSnapshot, p1: ObservedP1Features
) -> Decimal | None:
    if PaperDataField.TERM_STRUCTURE not in p1.present or not p1.term_atm_iv:
        return None
    cheapest = min(value for _expiry, value in p1.term_atm_iv)
    atm = p1.atm_iv(candidate.contract.expiry)
    if atm is None or atm <= 0:
        return None
    return _clamp(cheapest / atm, _ZERO, _ONE)


def _p1_rv_score(
    candidate: FeatureSnapshot, p1: ObservedP1Features, *, role: str
) -> Decimal | None:
    if PaperDataField.REALIZED_VOLATILITY not in p1.present:
        return None
    iv = _implied_vol(candidate)
    rv = p1.realized_volatility_annualized
    if iv is None or rv is None or rv <= 0:
        return None
    relative = iv / rv
    if role == "short":
        return _clamp(relative - Decimal("0.50"), _ZERO, _ONE)
    return _clamp(Decimal(2) - relative, _ZERO, _ONE)


def _p1_depth_score(
    candidate: FeatureSnapshot,
    universe: tuple[FeatureSnapshot, ...],
    p1: ObservedP1Features,
) -> Decimal | None:
    if PaperDataField.DEPTH not in p1.present:
        return None
    observed = [size for _symbol, size in p1.depth_top_size]
    if not observed:
        return None
    this = _top_size(candidate)
    if this is None or this <= 0:
        return _ZERO
    return _clamp(Decimal(this) / Decimal(max(observed)), _ZERO, _ONE)


def _p1_greeks_score(
    candidate: FeatureSnapshot,
    universe: tuple[FeatureSnapshot, ...],
    p1: ObservedP1Features,
) -> Decimal | None:
    if PaperDataField.GREEKS not in p1.present:
        return None
    greeks = None if candidate.derivatives is None else candidate.derivatives.greeks
    if greeks is None:
        return None
    parts: list[Decimal] = []
    theta = greeks.theta
    if theta is not None:
        peak = max((_abs_theta(item) or _ZERO) for item in universe)
        if peak > 0:
            parts.append(_clamp(_ONE - abs(theta) / peak, _ZERO, _ONE))
    vega = greeks.vega
    if vega is not None:
        peak_vega = max((_abs_vega(item) or _ZERO) for item in universe)
        if peak_vega > 0:
            parts.append(_clamp(abs(vega) / peak_vega, _ZERO, _ONE))
    if not parts:
        return None
    return sum(parts, _ZERO) / Decimal(len(parts))


def _top_size(candidate: FeatureSnapshot) -> int | None:
    return top_book_size(candidate)


def _abs_theta(candidate: FeatureSnapshot) -> Decimal | None:
    if candidate.derivatives is None or candidate.derivatives.greeks is None:
        return None
    theta = candidate.derivatives.greeks.theta
    return None if theta is None else abs(theta)


def _abs_vega(candidate: FeatureSnapshot) -> Decimal | None:
    if candidate.derivatives is None or candidate.derivatives.greeks is None:
        return None
    vega = candidate.derivatives.greeks.vega
    return None if vega is None else abs(vega)


def _implied_vol(candidate: FeatureSnapshot) -> Decimal | None:
    derivatives = candidate.derivatives
    if derivatives is None or derivatives.greeks is None:
        return None
    return derivatives.greeks.implied_volatility


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(max(value, low), high)


def _setup(
    market: MarketState,
    candidate: FeatureSnapshot,
    *,
    structure: StructureKind,
    score: Decimal,
    policy: IdentificationPolicy,
    rejected: tuple[str, ...],
    p1: ObservedP1Features | None = None,
) -> SetupFeatures:
    derivatives = candidate.derivatives
    if derivatives is None:
        raise ValueError("setup features require derivatives context")
    greeks = derivatives.greeks
    spread = _spread_fraction(candidate)
    if spread is None:
        raise ValueError("setup features require a valid observed spread")
    return SetupFeatures(
        identification_rule_version=policy.policy_version,
        router_version=policy.router_version,
        market_state_id=market.market_state_id,
        raw_setup_score=score,
        score_components={
            "contract_binding": score,
            "trend_strength": abs(market.trend_score or _ZERO),
        },
        trend=market.trend,
        volatility=market.volatility,
        structure=structure,
        dte=derivatives.days_to_expiry,
        delta=None if greeks is None else greeks.delta,
        implied_volatility=None if greeks is None else greeks.implied_volatility,
        iv_percentile=market.iv_percentile,
        iv_rv_ratio=market.iv_rv_ratio,
        open_interest=derivatives.open_interest,
        spread_fraction=spread,
        liquidity_rank=score,
        event_state=market.event_state,
        macro_status=market.macro_status,
        rejected_alternatives=rejected,
        p1_fields_used=() if p1 is None else tuple(item.value for item in p1.present),
        p1_fields_absent=() if p1 is None else tuple(item.value for item in p1.absent),
        p1_absence_reasons=() if p1 is None else p1.reason_labels(),
    )


def _direction_type(market: MarketState) -> OptionType | None:
    if market.trend.value == "UP":
        return OptionType.CALL
    if market.trend.value == "DOWN":
        return OptionType.PUT
    return None


def _abs_delta(candidate: FeatureSnapshot) -> Decimal | None:
    derivatives = candidate.derivatives
    if derivatives is None or derivatives.greeks is None:
        return None
    delta = derivatives.greeks.delta
    return None if delta is None else abs(delta)


def _spread_fraction(candidate: FeatureSnapshot) -> Decimal | None:
    bid, ask = candidate.market.bid, candidate.market.ask
    if bid is None or ask is None:
        return None
    mid = (bid.value + ask.value) / 2
    return None if mid <= 0 else (ask.value - bid.value) / mid


def _ratio(value: int | None, maximum: int) -> Decimal:
    if value is None or maximum <= 0:
        return _ZERO
    return min(_ONE, Decimal(value) / Decimal(maximum))


def _dominant_reason(
    candidates: tuple[FeatureSnapshot, ...], policy: IdentificationPolicy
) -> ReasonCode:
    reasons = [
        reason for item in candidates if (reason := _common_reason(item, policy))
    ]
    if not candidates:
        return ReasonCode.DATA_GAP
    if reasons:
        return reasons[0]
    return ReasonCode.INSTRUMENT_UNKNOWN


def _q(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"))
