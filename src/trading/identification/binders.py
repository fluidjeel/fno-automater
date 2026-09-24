"""Pure option-chain binders for single-leg and debit-spread strategies."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import cast

from trading.domain.contracts import CandidateBinding, FeatureSnapshot, MarketState
from trading.domain.contracts.identification import (
    SetupFeatures,
    StructureKind,
    TrendState,
    VolatilityState,
)
from trading.domain.contracts.paper_data import PaperDataField
from trading.domain.enums import FamilyId, InstrumentKind, OptionType, ReasonCode
from trading.identification.calendar import (
    M2ExpirySelection,
    TradingCalendarPort,
    get_calendar_port,
)
from trading.identification.config import IdentificationPolicy
from trading.identification.p1_features import ObservedP1Features, top_book_size

__all__ = [
    "BoundCandidates",
    "bind_credit_spread",
    "bind_debit_spread",
    "bind_iron_condor",
    "bind_long_call_butterfly",
    "bind_long_call_calendar",
    "bind_long_option",
    "bind_long_put_butterfly",
    "bind_long_put_calendar",
    "bind_long_straddle",
    "bind_long_strangle",
    "bind_m1_cas_option",
    "bind_m2_long_option",
    "bind_short_iron_butterfly",
]

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
    calendar: TradingCalendarPort | None = None,
    master_symbols: frozenset[str] | None = None,
    p1: ObservedP1Features | None = None,
) -> BoundCandidates:
    all_candidates = candidates
    if master_symbols is not None:
        in_master = tuple(
            item for item in candidates if item.contract.symbol in master_symbols
        )
        if not in_master:
            return BoundCandidates(
                binding=CandidateBinding(
                    strategy_id="positional_long_option",
                    binding_version=policy.binding_version,
                    selected_symbols=(),
                    score=_ZERO,
                    eligible=False,
                    reason_codes=(ReasonCode.INSTRUMENT_MASTER_ABSENT,),
                    rejected_symbols=tuple(
                        sorted(c.contract.symbol for c in all_candidates)
                    ),
                ),
                candidates=(),
                setup_features=None,
            )
        candidates = in_master

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
        sorted(item.contract.symbol for item in all_candidates if item not in eligible)
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


def _credit_option_type(
    family_id: FamilyId | None,
    market: MarketState,
) -> OptionType | None:
    if family_id == FamilyId.bull_put_credit:
        return OptionType.PUT
    if family_id == FamilyId.bear_call_credit:
        return OptionType.CALL
    trend = market.trend.value if hasattr(market.trend, "value") else str(market.trend)
    if trend == "UP":
        return OptionType.PUT
    if trend == "DOWN":
        return OptionType.CALL
    return None


def _order_credit_pair(
    first: FeatureSnapshot,
    second: FeatureSnapshot,
    option_type: OptionType,
) -> tuple[FeatureSnapshot, FeatureSnapshot] | None:
    if first.contract.expiry != second.contract.expiry:
        return None
    s1, s2 = first.contract.strike, second.contract.strike
    if s1 is None or s2 is None or s1 == s2:
        return None
    if option_type is OptionType.PUT:
        return (first, second) if s1 < s2 else (second, first)
    return (first, second) if s1 > s2 else (second, first)


def bind_credit_spread(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    family_id: FamilyId | None = None,
    master_symbols: frozenset[str] | None = None,
    p1: ObservedP1Features | None = None,
    max_risk_ratio: Decimal | None = None,
) -> BoundCandidates:
    strategy_id = family_id.value if family_id else "credit_spread"
    all_candidates = candidates
    if master_symbols is not None:
        in_master = tuple(
            item for item in candidates if item.contract.symbol in master_symbols
        )
        if not in_master:
            return BoundCandidates(
                binding=CandidateBinding(
                    strategy_id=strategy_id,
                    binding_version=policy.binding_version,
                    selected_symbols=(),
                    score=_ZERO,
                    eligible=False,
                    reason_codes=(ReasonCode.INSTRUMENT_MASTER_ABSENT,),
                    rejected_symbols=tuple(
                        sorted(c.contract.symbol for c in all_candidates)
                    ),
                ),
                candidates=(),
                setup_features=None,
            )
        candidates = in_master

    option_type = _credit_option_type(family_id, market)
    if option_type is None:
        rejected = tuple(sorted(item.contract.symbol for item in all_candidates))
        return BoundCandidates(
            binding=CandidateBinding(
                strategy_id=strategy_id,
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

    legs = [
        item
        for item in candidates
        if item.contract.option_type is option_type
        and _common_reason(item, policy) is None
        and _dte_ok(item, policy)
    ]

    pairs: list[tuple[Decimal, FeatureSnapshot, FeatureSnapshot]] = []
    for i in range(len(legs)):
        for j in range(i + 1, len(legs)):
            ordered = _order_credit_pair(legs[i], legs[j], option_type)
            if ordered is None:
                continue
            long_leg, short_leg = ordered
            if not _valid_credit_pair(
                long_leg,
                short_leg,
                option_type,
                policy,
                max_risk_ratio=max_risk_ratio,
            ):
                continue

            long_score = _candidate_score(
                long_leg,
                candidates,
                policy,
                delta_range=(
                    Decimal("0.05"),
                    policy.contracts.short_delta_max,
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
        rejected = tuple(sorted(item.contract.symbol for item in all_candidates))
        return BoundCandidates(
            binding=CandidateBinding(
                strategy_id=strategy_id,
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
            for item in all_candidates
            if item.contract.symbol not in selected_symbols
        )
    )
    binding = CandidateBinding(
        strategy_id=strategy_id,
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
            structure=StructureKind.CREDIT_SPREAD,
            score=score,
            policy=policy,
            rejected=rejected,
            p1=p1,
        ),
    )


def bind_iron_condor(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    master_symbols: frozenset[str] | None = None,
    p1: ObservedP1Features | None = None,
) -> BoundCandidates:
    """Bind a four-leg short iron condor for range-bound M4 candidates."""
    strategy_id = FamilyId.short_iron_condor_defined.value
    all_candidates = candidates
    if master_symbols is not None:
        in_master = tuple(
            item for item in candidates if item.contract.symbol in master_symbols
        )
        if not in_master:
            return BoundCandidates(
                binding=CandidateBinding(
                    strategy_id=strategy_id,
                    binding_version=policy.binding_version,
                    selected_symbols=(),
                    score=_ZERO,
                    eligible=False,
                    reason_codes=(ReasonCode.INSTRUMENT_MASTER_ABSENT,),
                    rejected_symbols=tuple(
                        sorted(c.contract.symbol for c in all_candidates)
                    ),
                ),
                candidates=(),
                setup_features=None,
            )
        candidates = in_master

    if market.trend is not TrendState.RANGE:
        return BoundCandidates(
            binding=CandidateBinding(
                strategy_id=strategy_id,
                binding_version=policy.binding_version,
                selected_symbols=(),
                score=_ZERO,
                eligible=False,
                reason_codes=(ReasonCode.DATA_INVALID,),
                rejected_symbols=tuple(
                    sorted(c.contract.symbol for c in all_candidates)
                ),
            ),
            candidates=(),
            setup_features=None,
        )

    puts = [
        item
        for item in candidates
        if item.contract.option_type is OptionType.PUT
        and _common_reason(item, policy) is None
        and _dte_ok(item, policy)
    ]
    calls = [
        item
        for item in candidates
        if item.contract.option_type is OptionType.CALL
        and _common_reason(item, policy) is None
        and _dte_ok(item, policy)
    ]

    combos: list[
        tuple[
            Decimal, FeatureSnapshot, FeatureSnapshot, FeatureSnapshot, FeatureSnapshot
        ]
    ] = []
    for i in range(len(puts)):
        for j in range(i + 1, len(puts)):
            lp, sp = sorted(
                (puts[i], puts[j]), key=lambda item: item.contract.strike or _ZERO
            )
            if lp.contract.strike is None or sp.contract.strike is None:
                continue
            if lp.contract.strike >= sp.contract.strike:
                continue
            for k in range(len(calls)):
                for m in range(k + 1, len(calls)):
                    sc, lc = sorted(
                        (calls[k], calls[m]),
                        key=lambda item: item.contract.strike or _ZERO,
                    )
                    if sc.contract.strike is None or lc.contract.strike is None:
                        continue
                    if sc.contract.strike >= lc.contract.strike:
                        continue
                    if not (
                        lp.contract.strike
                        < sp.contract.strike
                        < sc.contract.strike
                        < lc.contract.strike
                    ):
                        continue
                    put_width = sp.contract.strike - lp.contract.strike
                    call_width = lc.contract.strike - sc.contract.strike
                    width_penalty = abs(put_width - call_width)
                    score = (
                        _candidate_score(
                            lp,
                            candidates,
                            policy,
                            delta_range=(Decimal("0.05"), Decimal("0.25")),
                            p1=p1,
                            role="long",
                        )
                        + _candidate_score(
                            sp,
                            candidates,
                            policy,
                            delta_range=(
                                policy.contracts.short_delta_min,
                                policy.contracts.short_delta_max,
                            ),
                            p1=p1,
                            role="short",
                        )
                        + _candidate_score(
                            sc,
                            candidates,
                            policy,
                            delta_range=(
                                policy.contracts.short_delta_min,
                                policy.contracts.short_delta_max,
                            ),
                            p1=p1,
                            role="short",
                        )
                        + _candidate_score(
                            lc,
                            candidates,
                            policy,
                            delta_range=(Decimal("0.05"), Decimal("0.25")),
                            p1=p1,
                            role="long",
                        )
                    ) / Decimal(4)
                    score -= width_penalty / Decimal("1000")
                    combos.append((_q(score), lp, sp, sc, lc))

    combos.sort(
        key=lambda row: (
            -row[0],
            row[1].contract.symbol,
            row[2].contract.symbol,
            row[3].contract.symbol,
            row[4].contract.symbol,
        )
    )

    if not combos:
        return BoundCandidates(
            binding=CandidateBinding(
                strategy_id=strategy_id,
                binding_version=policy.binding_version,
                selected_symbols=(),
                score=_ZERO,
                eligible=False,
                reason_codes=(_dominant_reason(candidates, policy),),
                rejected_symbols=tuple(
                    sorted(c.contract.symbol for c in all_candidates)
                ),
            ),
            candidates=(),
            setup_features=None,
        )

    score, long_put, short_put, short_call, long_call = combos[0]
    selected_symbols = (
        long_put.contract.symbol,
        short_put.contract.symbol,
        short_call.contract.symbol,
        long_call.contract.symbol,
    )
    rejected = tuple(
        sorted(
            item.contract.symbol
            for item in all_candidates
            if item.contract.symbol not in selected_symbols
        )
    )
    return BoundCandidates(
        binding=CandidateBinding(
            strategy_id=strategy_id,
            binding_version=policy.binding_version,
            selected_symbols=selected_symbols,
            score=score,
            eligible=True,
            rejected_symbols=rejected,
        ),
        candidates=(long_put, short_put, short_call, long_call),
        setup_features=_setup(
            market,
            long_put,
            structure=StructureKind.CREDIT_SPREAD,
            score=score,
            policy=policy,
            rejected=rejected,
            p1=p1,
        ),
    )


def bind_long_call_butterfly(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    master_symbols: frozenset[str] | None = None,
    p1: ObservedP1Features | None = None,
) -> BoundCandidates:
    """Bind a symmetric long call butterfly for range-bound M4."""
    return _bind_long_butterfly(
        candidates,
        market=market,
        policy=policy,
        option_type=OptionType.CALL,
        family_id=FamilyId.long_call_butterfly.value,
        master_symbols=master_symbols,
        p1=p1,
    )


def bind_long_put_butterfly(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    master_symbols: frozenset[str] | None = None,
    p1: ObservedP1Features | None = None,
) -> BoundCandidates:
    """Bind a symmetric long put butterfly for range-bound M4."""
    return _bind_long_butterfly(
        candidates,
        market=market,
        policy=policy,
        option_type=OptionType.PUT,
        family_id=FamilyId.long_put_butterfly.value,
        master_symbols=master_symbols,
        p1=p1,
    )


def bind_short_iron_butterfly(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    master_symbols: frozenset[str] | None = None,
    p1: ObservedP1Features | None = None,
) -> BoundCandidates:
    """Bind a short iron butterfly for range-bound M4."""
    strategy_id = FamilyId.short_iron_butterfly_defined.value
    all_candidates = candidates
    if master_symbols is not None:
        in_master = tuple(
            item for item in candidates if item.contract.symbol in master_symbols
        )
        if not in_master:
            return _ineligible_binding(
                strategy_id=strategy_id,
                policy=policy,
                all_candidates=all_candidates,
                reason_codes=(ReasonCode.INSTRUMENT_MASTER_ABSENT,),
            )
        candidates = in_master
    if market.trend is not TrendState.RANGE:
        return _ineligible_binding(
            strategy_id=strategy_id,
            policy=policy,
            all_candidates=all_candidates,
            reason_codes=(ReasonCode.DATA_INVALID,),
        )

    puts = [
        item
        for item in candidates
        if item.contract.option_type is OptionType.PUT
        and _common_reason(item, policy) is None
        and _dte_ok(item, policy)
    ]
    calls = [
        item
        for item in candidates
        if item.contract.option_type is OptionType.CALL
        and _common_reason(item, policy) is None
        and _dte_ok(item, policy)
    ]
    combos: list[
        tuple[
            Decimal, FeatureSnapshot, FeatureSnapshot, FeatureSnapshot, FeatureSnapshot
        ]
    ] = []
    for i in range(len(puts)):
        for j in range(i + 1, len(puts)):
            lp, sp = sorted(
                (puts[i], puts[j]), key=lambda item: item.contract.strike or _ZERO
            )
            if lp.contract.strike is None or sp.contract.strike is None:
                continue
            for k in range(len(calls)):
                for m in range(k + 1, len(calls)):
                    sc, lc = sorted(
                        (calls[k], calls[m]),
                        key=lambda item: item.contract.strike or _ZERO,
                    )
                    if sc.contract.strike is None or lc.contract.strike is None:
                        continue
                    if sp.contract.strike != sc.contract.strike:
                        continue
                    if not (
                        lp.contract.strike < sp.contract.strike < lc.contract.strike
                    ):
                        continue
                    score = (
                        _candidate_score(lp, candidates, policy, p1=p1, role="long")
                        + _candidate_score(sp, candidates, policy, p1=p1, role="short")
                        + _candidate_score(sc, candidates, policy, p1=p1, role="short")
                        + _candidate_score(lc, candidates, policy, p1=p1, role="long")
                    ) / Decimal(4)
                    combos.append((_q(score), lp, sp, sc, lc))

    combos.sort(
        key=lambda row: (
            -row[0],
            row[1].contract.symbol,
            row[2].contract.symbol,
            row[3].contract.symbol,
            row[4].contract.symbol,
        )
    )
    if not combos:
        return _ineligible_binding(
            strategy_id=strategy_id,
            policy=policy,
            all_candidates=all_candidates,
            reason_codes=(_dominant_reason(candidates, policy),),
        )

    score, long_put, short_put, short_call, long_call = combos[0]
    selected_symbols = (
        long_put.contract.symbol,
        long_call.contract.symbol,
        short_put.contract.symbol,
        short_call.contract.symbol,
    )
    rejected = _rejected_symbols(all_candidates, selected_symbols)
    return BoundCandidates(
        binding=CandidateBinding(
            strategy_id=strategy_id,
            binding_version=policy.binding_version,
            selected_symbols=selected_symbols,
            score=score,
            eligible=True,
            rejected_symbols=rejected,
        ),
        candidates=(long_put, long_call, short_put, short_call),
        setup_features=_setup(
            market,
            long_put,
            structure=StructureKind.CREDIT_SPREAD,
            score=score,
            policy=policy,
            rejected=rejected,
            p1=p1,
        ),
    )


def bind_long_straddle(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    master_symbols: frozenset[str] | None = None,
    p1: ObservedP1Features | None = None,
) -> BoundCandidates:
    """Bind an ATM long straddle when volatility is not compressed."""
    return _bind_long_volatility_pair(
        candidates,
        market=market,
        policy=policy,
        family_id=FamilyId.long_straddle.value,
        same_strike=True,
        master_symbols=master_symbols,
        p1=p1,
    )


def bind_long_strangle(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    master_symbols: frozenset[str] | None = None,
    p1: ObservedP1Features | None = None,
) -> BoundCandidates:
    """Bind an OTM long strangle when volatility is not compressed."""
    return _bind_long_volatility_pair(
        candidates,
        market=market,
        policy=policy,
        family_id=FamilyId.long_strangle.value,
        same_strike=False,
        master_symbols=master_symbols,
        p1=p1,
    )


def bind_long_call_calendar(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    master_symbols: frozenset[str] | None = None,
    p1: ObservedP1Features | None = None,
) -> BoundCandidates:
    """Bind a research long call calendar (short near / long far, same strike)."""
    return _bind_calendar_pair(
        candidates,
        market=market,
        policy=policy,
        option_type=OptionType.CALL,
        family_id=FamilyId.long_call_calendar.value,
        master_symbols=master_symbols,
        p1=p1,
    )


def bind_long_put_calendar(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    master_symbols: frozenset[str] | None = None,
    p1: ObservedP1Features | None = None,
) -> BoundCandidates:
    """Bind a research long put calendar (short near / long far, same strike)."""
    return _bind_calendar_pair(
        candidates,
        market=market,
        policy=policy,
        option_type=OptionType.PUT,
        family_id=FamilyId.long_put_calendar.value,
        master_symbols=master_symbols,
        p1=p1,
    )


def _bind_calendar_pair(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    option_type: OptionType,
    family_id: str,
    master_symbols: frozenset[str] | None,
    p1: ObservedP1Features | None,
) -> BoundCandidates:
    all_candidates = candidates
    if master_symbols is not None:
        in_master = tuple(
            item for item in candidates if item.contract.symbol in master_symbols
        )
        if not in_master:
            return _ineligible_binding(
                strategy_id=family_id,
                policy=policy,
                all_candidates=all_candidates,
                reason_codes=(ReasonCode.INSTRUMENT_MASTER_ABSENT,),
            )
        candidates = in_master
    if market.trend is not TrendState.RANGE:
        return _ineligible_binding(
            strategy_id=family_id,
            policy=policy,
            all_candidates=all_candidates,
            reason_codes=(ReasonCode.DATA_INVALID,),
        )
    options = [
        item
        for item in candidates
        if item.contract.option_type is option_type
        and item.contract.strike is not None
        and item.contract.expiry is not None
        and item.derivatives is not None
    ]
    if len(options) < 2:
        return _ineligible_binding(
            strategy_id=family_id,
            policy=policy,
            all_candidates=all_candidates,
            reason_codes=(ReasonCode.INSTRUMENT_UNKNOWN,),
        )
    by_strike: dict[Decimal, list[FeatureSnapshot]] = {}
    for item in options:
        strike = item.contract.strike
        if strike is None:
            continue
        by_strike.setdefault(strike, []).append(item)
    pairs: list[tuple[Decimal, FeatureSnapshot, FeatureSnapshot]] = []
    for group in by_strike.values():
        ordered = sorted(
            group,
            key=lambda snap: (
                snap.derivatives.days_to_expiry if snap.derivatives else 999,
                snap.contract.expiry or date.max,
            ),
        )
        if len(ordered) < 2:
            continue
        near, far = ordered[0], ordered[-1]
        if near.contract.expiry == far.contract.expiry:
            continue
        near_dte = near.derivatives.days_to_expiry if near.derivatives else 0
        far_dte = far.derivatives.days_to_expiry if far.derivatives else 0
        if near_dte < 2 or far_dte <= near_dte:
            continue
        near_ask = near.market.ask.value if near.market.ask else None
        far_bid = far.market.bid.value if far.market.bid else None
        if near_ask is None or far_bid is None:
            continue
        debit = far_bid - near_ask
        if debit <= 0:
            continue
        atm_distance = _atm_distance_from_snapshot(near, near.contract.strike)
        score = _ONE - atm_distance
        pairs.append((score, near, far))
    if not pairs:
        return _ineligible_binding(
            strategy_id=family_id,
            policy=policy,
            all_candidates=all_candidates,
            reason_codes=(ReasonCode.INSTRUMENT_UNKNOWN,),
        )
    pairs.sort(key=lambda item: item[0], reverse=True)
    _, near, far = pairs[0]
    selected = {near.contract.symbol, far.contract.symbol}
    rejected = tuple(
        sorted(
            item.contract.symbol
            for item in all_candidates
            if item.contract.symbol not in selected
        )
    )
    binding = CandidateBinding(
        strategy_id=family_id,
        binding_version=policy.binding_version,
        selected_symbols=(near.contract.symbol, far.contract.symbol),
        score=pairs[0][0],
        eligible=True,
        rejected_symbols=rejected,
    )
    return BoundCandidates(
        binding=binding,
        candidates=(near, far),
        setup_features=_setup(
            market,
            near,
            structure=StructureKind.DEBIT_SPREAD,
            score=pairs[0][0],
            policy=policy,
            rejected=rejected,
            p1=p1,
        ),
    )


def _atm_distance_from_snapshot(
    snapshot: FeatureSnapshot, strike: Decimal | None
) -> Decimal:
    if (
        strike is None
        or snapshot.derivatives is None
        or snapshot.derivatives.underlying_price is None
    ):
        return _ONE
    underlying = snapshot.derivatives.underlying_price.value
    if underlying <= 0:
        return _ONE
    distance = abs(strike - underlying) / underlying
    return min(_ONE, distance)


def _bind_long_butterfly(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    option_type: OptionType,
    family_id: str,
    master_symbols: frozenset[str] | None,
    p1: ObservedP1Features | None,
) -> BoundCandidates:
    all_candidates = candidates
    if master_symbols is not None:
        in_master = tuple(
            item for item in candidates if item.contract.symbol in master_symbols
        )
        if not in_master:
            return _ineligible_binding(
                strategy_id=family_id,
                policy=policy,
                all_candidates=all_candidates,
                reason_codes=(ReasonCode.INSTRUMENT_MASTER_ABSENT,),
            )
        candidates = in_master
    if market.trend is not TrendState.RANGE:
        return _ineligible_binding(
            strategy_id=family_id,
            policy=policy,
            all_candidates=all_candidates,
            reason_codes=(ReasonCode.DATA_INVALID,),
        )

    options = [
        item
        for item in candidates
        if item.contract.option_type is option_type
        and _common_reason(item, policy) is None
        and _dte_ok(item, policy)
    ]
    combos: list[tuple[Decimal, FeatureSnapshot, FeatureSnapshot, FeatureSnapshot]] = []
    for i in range(len(options)):
        for j in range(i + 1, len(options)):
            for k in range(j + 1, len(options)):
                low, mid, high = sorted(
                    (options[i], options[j], options[k]),
                    key=lambda item: item.contract.strike or _ZERO,
                )
                if (
                    low.contract.strike is None
                    or mid.contract.strike is None
                    or high.contract.strike is None
                ):
                    continue
                if (
                    high.contract.strike - mid.contract.strike
                    != mid.contract.strike - low.contract.strike
                ):
                    continue
                score = (
                    _candidate_score(low, candidates, policy, p1=p1, role="long")
                    + _candidate_score(mid, candidates, policy, p1=p1, role="short")
                    + _candidate_score(high, candidates, policy, p1=p1, role="long")
                ) / Decimal(3)
                combos.append((_q(score), low, mid, high))

    combos.sort(
        key=lambda row: (
            -row[0],
            row[1].contract.symbol,
            row[2].contract.symbol,
            row[3].contract.symbol,
        )
    )
    if not combos:
        return _ineligible_binding(
            strategy_id=family_id,
            policy=policy,
            all_candidates=all_candidates,
            reason_codes=(_dominant_reason(candidates, policy),),
        )

    score, low, mid, high = combos[0]
    selected_symbols = (low.contract.symbol, mid.contract.symbol, high.contract.symbol)
    rejected = _rejected_symbols(all_candidates, selected_symbols)
    return BoundCandidates(
        binding=CandidateBinding(
            strategy_id=family_id,
            binding_version=policy.binding_version,
            selected_symbols=selected_symbols,
            score=score,
            eligible=True,
            rejected_symbols=rejected,
        ),
        candidates=(low, mid, high),
        setup_features=_setup(
            market,
            low,
            structure=StructureKind.DEBIT_SPREAD,
            score=score,
            policy=policy,
            rejected=rejected,
            p1=p1,
        ),
    )


def _bind_long_volatility_pair(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    family_id: str,
    same_strike: bool,
    master_symbols: frozenset[str] | None,
    p1: ObservedP1Features | None,
) -> BoundCandidates:
    all_candidates = candidates
    if master_symbols is not None:
        in_master = tuple(
            item for item in candidates if item.contract.symbol in master_symbols
        )
        if not in_master:
            return _ineligible_binding(
                strategy_id=family_id,
                policy=policy,
                all_candidates=all_candidates,
                reason_codes=(ReasonCode.INSTRUMENT_MASTER_ABSENT,),
            )
        candidates = in_master
    if market.volatility is VolatilityState.COMPRESSED:
        return _ineligible_binding(
            strategy_id=family_id,
            policy=policy,
            all_candidates=all_candidates,
            reason_codes=(ReasonCode.DATA_INVALID,),
        )

    puts = [
        item
        for item in candidates
        if item.contract.option_type is OptionType.PUT
        and _common_reason(item, policy) is None
        and _dte_ok(item, policy)
    ]
    calls = [
        item
        for item in candidates
        if item.contract.option_type is OptionType.CALL
        and _common_reason(item, policy) is None
        and _dte_ok(item, policy)
    ]
    combos: list[tuple[Decimal, FeatureSnapshot, FeatureSnapshot]] = []
    for put in puts:
        for call in calls:
            put_strike = put.contract.strike
            call_strike = call.contract.strike
            if put_strike is None or call_strike is None:
                continue
            if same_strike:
                if put_strike != call_strike:
                    continue
            elif put_strike >= call_strike:
                continue
            score = (
                _candidate_score(put, candidates, policy, p1=p1, role="long")
                + _candidate_score(call, candidates, policy, p1=p1, role="long")
            ) / Decimal(2)
            combos.append((_q(score), put, call))

    combos.sort(
        key=lambda row: (-row[0], row[1].contract.symbol, row[2].contract.symbol)
    )
    if not combos:
        return _ineligible_binding(
            strategy_id=family_id,
            policy=policy,
            all_candidates=all_candidates,
            reason_codes=(_dominant_reason(candidates, policy),),
        )

    score, put, call = combos[0]
    selected_symbols = (put.contract.symbol, call.contract.symbol)
    rejected = _rejected_symbols(all_candidates, selected_symbols)
    return BoundCandidates(
        binding=CandidateBinding(
            strategy_id=family_id,
            binding_version=policy.binding_version,
            selected_symbols=selected_symbols,
            score=score,
            eligible=True,
            rejected_symbols=rejected,
        ),
        candidates=(put, call),
        setup_features=_setup(
            market,
            put,
            structure=StructureKind.DEBIT_SPREAD,
            score=score,
            policy=policy,
            rejected=rejected,
            p1=p1,
        ),
    )


def _ineligible_binding(
    *,
    strategy_id: str,
    policy: IdentificationPolicy,
    all_candidates: tuple[FeatureSnapshot, ...],
    reason_codes: tuple[ReasonCode, ...],
) -> BoundCandidates:
    return BoundCandidates(
        binding=CandidateBinding(
            strategy_id=strategy_id,
            binding_version=policy.binding_version,
            selected_symbols=(),
            score=_ZERO,
            eligible=False,
            reason_codes=reason_codes,
            rejected_symbols=tuple(sorted(c.contract.symbol for c in all_candidates)),
        ),
        candidates=(),
        setup_features=None,
    )


def _rejected_symbols(
    all_candidates: tuple[FeatureSnapshot, ...],
    selected_symbols: tuple[str, ...],
) -> tuple[str, ...]:
    selected = set(selected_symbols)
    return tuple(
        sorted(
            item.contract.symbol
            for item in all_candidates
            if item.contract.symbol not in selected
        )
    )


def bind_m2_long_option(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    calendar: TradingCalendarPort | None = None,
    master_symbols: frozenset[str] | None = None,
    p1: ObservedP1Features | None = None,
    allow_fallback_expiry: bool = False,
) -> BoundCandidates:
    all_candidates = candidates
    if master_symbols is not None:
        in_master = tuple(
            item for item in candidates if item.contract.symbol in master_symbols
        )
        if not in_master:
            return BoundCandidates(
                binding=CandidateBinding(
                    strategy_id="positional_long_option",
                    binding_version=policy.binding_version,
                    selected_symbols=(),
                    score=_ZERO,
                    eligible=False,
                    reason_codes=(ReasonCode.INSTRUMENT_MASTER_ABSENT,),
                    rejected_symbols=tuple(
                        sorted(c.contract.symbol for c in all_candidates)
                    ),
                ),
                candidates=(),
                setup_features=None,
            )
        candidates = in_master

    option_type = _direction_type(market)
    if option_type is None:
        return BoundCandidates(
            binding=CandidateBinding(
                strategy_id="positional_long_option",
                binding_version=policy.binding_version,
                selected_symbols=(),
                score=_ZERO,
                eligible=False,
                reason_codes=(ReasonCode.PRICE_UNAVAILABLE,),
                rejected_symbols=tuple(
                    sorted(c.contract.symbol for c in all_candidates)
                ),
            ),
            candidates=(),
            setup_features=None,
        )

    cal = calendar or get_calendar_port()
    as_of = cal.session_date(market.calculated_at)
    listed_expiries = tuple(
        set(
            item.contract.expiry
            for item in candidates
            if item.contract.expiry is not None
        )
    )
    expiry_sel: M2ExpirySelection = cal.select_m2_expiry(
        listed_expiries, as_of=as_of, allow_fallback=allow_fallback_expiry
    )
    if not expiry_sel.eligible or expiry_sel.selected_expiry is None:
        return BoundCandidates(
            binding=CandidateBinding(
                strategy_id="positional_long_option",
                binding_version=policy.binding_version,
                selected_symbols=(),
                score=_ZERO,
                eligible=False,
                reason_codes=(expiry_sel.reason_code,),
                rejected_symbols=tuple(
                    sorted(c.contract.symbol for c in all_candidates)
                ),
            ),
            candidates=(),
            setup_features=None,
        )

    expiry_candidates = tuple(
        item
        for item in candidates
        if item.contract.expiry == expiry_sel.selected_expiry
    )
    eligible = [
        item
        for item in expiry_candidates
        if item.contract.option_type is option_type
        and _common_reason(item, policy) is None
        and _abs_delta_in_range(item, Decimal("0.45"), Decimal("0.65"))
    ]
    if not eligible:
        return BoundCandidates(
            binding=CandidateBinding(
                strategy_id="positional_long_option",
                binding_version=policy.binding_version,
                selected_symbols=(),
                score=_ZERO,
                eligible=False,
                reason_codes=(
                    _dominant_reason(expiry_candidates or candidates, policy),
                ),
                rejected_symbols=tuple(
                    sorted(c.contract.symbol for c in all_candidates)
                ),
            ),
            candidates=(),
            setup_features=None,
        )

    ranked = sorted(
        (
            (
                _candidate_score(
                    item,
                    candidates,
                    policy,
                    delta_range=(Decimal("0.45"), Decimal("0.65")),
                    p1=p1,
                ),
                item,
            )
            for item in eligible
        ),
        key=lambda pair: (-pair[0], pair[1].contract.symbol),
    )
    score, selected = ranked[0]
    selected_symbol = selected.contract.symbol
    rejected = tuple(
        sorted(
            item.contract.symbol
            for item in all_candidates
            if item.contract.symbol != selected_symbol
        )
    )
    binding = CandidateBinding(
        strategy_id="positional_long_option",
        binding_version=policy.binding_version,
        selected_symbols=(selected_symbol,),
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
            dte=expiry_sel.dte,
            extra_score_components={
                "m2_dte": Decimal(expiry_sel.dte or 0),
                "is_holiday_substituted": Decimal(
                    1 if expiry_sel.is_holiday_substituted else 0
                ),
                "is_monthly_substituted": Decimal(
                    1 if expiry_sel.is_monthly_substituted else 0
                ),
            },
        ),
    )


def bind_m1_cas_option(
    candidates: tuple[FeatureSnapshot, ...],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    calendar: TradingCalendarPort | None = None,
    master_symbols: frozenset[str] | None = None,
    p1: ObservedP1Features | None = None,
    allow_0dte: bool = False,
    delta_range: tuple[Decimal, Decimal] = (Decimal("0.20"), Decimal("0.40")),
) -> BoundCandidates:
    all_candidates = candidates
    if master_symbols is not None:
        in_master = tuple(
            item for item in candidates if item.contract.symbol in master_symbols
        )
        if not in_master:
            return BoundCandidates(
                binding=CandidateBinding(
                    strategy_id="cas_microstructure",
                    binding_version=policy.binding_version,
                    selected_symbols=(),
                    score=_ZERO,
                    eligible=False,
                    reason_codes=(ReasonCode.INSTRUMENT_MASTER_ABSENT,),
                    rejected_symbols=tuple(
                        sorted(c.contract.symbol for c in all_candidates)
                    ),
                ),
                candidates=(),
                setup_features=None,
            )
        candidates = in_master

    option_type = _direction_type(market)
    if option_type is None:
        return BoundCandidates(
            binding=CandidateBinding(
                strategy_id="cas_microstructure",
                binding_version=policy.binding_version,
                selected_symbols=(),
                score=_ZERO,
                eligible=False,
                reason_codes=(ReasonCode.PRICE_UNAVAILABLE,),
                rejected_symbols=tuple(
                    sorted(c.contract.symbol for c in all_candidates)
                ),
            ),
            candidates=(),
            setup_features=None,
        )

    cal = calendar or get_calendar_port()
    as_of = cal.session_date(market.calculated_at)

    def _is_zero_dte(item: FeatureSnapshot) -> bool:
        return bool(
            (item.derivatives is not None and item.derivatives.days_to_expiry == 0)
            or (
                item.contract.expiry is not None
                and (item.contract.expiry - as_of).days == 0
            )
        )

    if not allow_0dte:
        dte_candidates = tuple(item for item in candidates if not _is_zero_dte(item))
    else:
        dte_candidates = candidates

    min_delta, max_delta = delta_range
    eligible = [
        item
        for item in dte_candidates
        if item.contract.option_type is option_type
        and _common_reason(item, policy) is None
        and _abs_delta_in_range(item, min_delta, max_delta)
    ]
    if not eligible:
        if candidates and not dte_candidates:
            reason = ReasonCode.EXPIRY_0_1_DTE_EXCLUDED
        else:
            reason = _dominant_reason(dte_candidates or candidates, policy)
        return BoundCandidates(
            binding=CandidateBinding(
                strategy_id="cas_microstructure",
                binding_version=policy.binding_version,
                selected_symbols=(),
                score=_ZERO,
                eligible=False,
                reason_codes=(reason,),
                rejected_symbols=tuple(
                    sorted(c.contract.symbol for c in all_candidates)
                ),
            ),
            candidates=(),
            setup_features=None,
        )

    ranked = sorted(
        (
            (
                _candidate_score(
                    item,
                    candidates,
                    policy,
                    delta_range=delta_range,
                    p1=p1,
                ),
                item,
            )
            for item in eligible
        ),
        key=lambda pair: (-pair[0], pair[1].contract.symbol),
    )
    score, selected = ranked[0]
    selected_symbol = selected.contract.symbol
    rejected = tuple(
        sorted(
            item.contract.symbol
            for item in all_candidates
            if item.contract.symbol != selected_symbol
        )
    )
    binding = CandidateBinding(
        strategy_id="cas_microstructure",
        binding_version=policy.binding_version,
        selected_symbols=(selected_symbol,),
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
            structure=StructureKind.CAS_OPTION,
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
    return _abs_delta_in_range(
        candidate,
        policy.contracts.long_delta_min,
        policy.contracts.long_delta_max,
    )


def _short_delta_ok(candidate: FeatureSnapshot, policy: IdentificationPolicy) -> bool:
    return _abs_delta_in_range(
        candidate,
        policy.contracts.short_delta_min,
        policy.contracts.short_delta_max,
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


def _valid_credit_pair(  # noqa: PLR0911 - invalid payoff conditions fail closed
    long_leg: FeatureSnapshot,
    short_leg: FeatureSnapshot,
    option_type: OptionType,
    policy: IdentificationPolicy,
    *,
    max_risk_ratio: Decimal | None = None,
) -> bool:
    if long_leg.contract.expiry != short_leg.contract.expiry:
        return False
    long_strike, short_strike = long_leg.contract.strike, short_leg.contract.strike
    if long_strike is None or short_strike is None or long_strike == short_strike:
        return False
    if option_type is OptionType.PUT and long_strike >= short_strike:
        return False
    if option_type is OptionType.CALL and long_strike <= short_strike:
        return False
    long_ask, short_bid = long_leg.market.ask, short_leg.market.bid
    if long_ask is None or short_bid is None:
        return False
    lot = long_leg.features.get("lot_size")
    if lot is None or lot <= 0:
        return False
    cost_points = policy.contracts.estimated_round_trip_cost_per_lot / lot
    net_credit = short_bid.value - long_ask.value - cost_points
    width = abs(short_strike - long_strike)
    if net_credit <= 0 or net_credit >= width:
        return False
    cap = max_risk_ratio
    if cap is None:
        cap = getattr(policy.contracts, "max_risk_ratio", Decimal("4.0"))
    risk = width - net_credit
    return (risk / net_credit) <= cap


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
    dte: int | None = None,
    extra_score_components: dict[str, Decimal] | None = None,
) -> SetupFeatures:
    derivatives = candidate.derivatives
    if derivatives is None:
        raise ValueError("setup features require derivatives context")
    greeks = derivatives.greeks
    spread = _spread_fraction(candidate)
    if spread is None:
        raise ValueError("setup features require a valid observed spread")
    score_components = {
        "contract_binding": score,
        "trend_strength": abs(market.trend_score or _ZERO),
    }
    if extra_score_components:
        score_components.update(extra_score_components)
    resolved_dte = dte if dte is not None else derivatives.days_to_expiry
    return SetupFeatures(
        identification_rule_version=policy.policy_version,
        router_version=policy.router_version,
        market_state_id=market.market_state_id,
        raw_setup_score=score,
        score_components=score_components,
        trend=market.trend,
        volatility=market.volatility,
        structure=structure,
        dte=resolved_dte,
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


def _abs_delta_in_range(
    candidate: FeatureSnapshot, min_delta: Decimal, max_delta: Decimal
) -> bool:
    delta = _abs_delta(candidate)
    return delta is not None and min_delta <= delta <= max_delta


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
