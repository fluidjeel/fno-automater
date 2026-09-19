"""Deterministic one-winner NIFTY option structure router.

Live-strict mode (``paper_fail_fast: false``) still abstains on freeze gates.
Paper fail-fast names a family whenever market state exists so the paper stack
can learn from outcomes. It does not ENABLE live OMS, write family_actions, or
call a broker.

Paper still does not:
- route ``commodity_futures_trend`` on this NIFTY desk
- pick CAS outside auction windows
- submit when Layer 2 / readiness freezes entries
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from trading.domain.contracts import CandidateBinding, MarketState, RouteDecision
from trading.domain.contracts.identification import (
    MacroStatus,
    PaperTenor,
    SetupFeatures,
    TrendState,
)
from trading.domain.enums import ReasonCode
from trading.identification.allow_table import (
    SessionBucket,
    allowed_families_for,
    paper_fallback_families,
    session_bucket_for,
)
from trading.identification.binders import BoundCandidates
from trading.identification.config import IdentificationPolicy

__all__ = ["RoutedOpportunity", "route_nifty_options"]

_FAMILY_ORDER = (
    "cas_microstructure",
    "defined_risk_multileg",
    "positional_long_option",
    "debit_spread",
)
_CAS_PRIOR_BUMP = Decimal("0.10")
_MULTILEG_PRIOR_BUMP = Decimal("0.02")
_ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class RoutedOpportunity:
    strategy_id: str
    execution: bool
    bound: BoundCandidates
    setup_features: SetupFeatures | None


def route_nifty_options(
    market: MarketState,
    *,
    long_option: BoundCandidates,
    debit_spread: BoundCandidates,
    policy: IdentificationPolicy,
    entries_permitted: bool = True,
    existing_correlated_exposure: bool = False,
    cooldown_active: bool = False,
    allowed_families: frozenset[str] | None = None,
    cas_microstructure: BoundCandidates | None = None,
    defined_risk_multileg: BoundCandidates | None = None,
) -> tuple[RouteDecision, tuple[RoutedOpportunity, ...]]:
    fail_fast = policy.router.paper_fail_fast
    families = (
        allowed_families
        if allowed_families is not None
        else allowed_families_for(market, policy)
    )
    if fail_fast and allowed_families is None and not families:
        families = paper_fallback_families(market, policy)

    supplied = {
        "positional_long_option": long_option,
        "debit_spread": debit_spread,
    }
    if cas_microstructure is not None:
        supplied["cas_microstructure"] = cas_microstructure
    if defined_risk_multileg is not None:
        supplied["defined_risk_multileg"] = defined_risk_multileg
    effective = _effective_candidates(supplied, families, policy)

    hard_reasons, soft_reasons = _gate_reasons(
        market,
        policy,
        entries_permitted=entries_permitted,
        existing_correlated_exposure=existing_correlated_exposure,
        cooldown_active=cooldown_active,
    )
    session = session_bucket_for(market.calculated_at, policy)
    preferred = _preferred(market, policy, families, session=session)
    if preferred is not None and preferred not in families:
        (soft_reasons if fail_fast else hard_reasons).append(ReasonCode.STRATEGY_HALTED)

    scored = sorted(
        (
            (name, item.binding.score)
            for name, item in effective.items()
            if item.binding.eligible and name in families
        ),
        key=lambda pair: (-pair[1], pair[0]),
    )
    winner, winner_score, score_gap, extra_gates, forced = _pick_winner(
        preferred=preferred,
        families=families,
        effective=effective,
        scored=scored,
        policy=policy,
        hard_reasons=hard_reasons,
    )
    if fail_fast and winner is not None and soft_reasons:
        forced = True

    shadows = tuple(name for name, _score in scored if name != winner)
    rejected = tuple(
        sorted(
            name
            for name in effective
            if name not in families or not effective[name].binding.eligible
        )
    )
    failed_gates = [reason.value for reason in (*hard_reasons, *soft_reasons)]
    failed_gates.extend(extra_gates)
    if winner is None and not hard_reasons and not extra_gates:
        failed_gates.append("no_clear_winner")

    tenor = _paper_tenor(
        winner,
        None if winner is None else effective.get(winner),
        policy,
    )
    route_key = (
        f"{policy.router_version}|{market.market_state_id}|{winner}|{'|'.join(shadows)}"
    )
    decision = RouteDecision(
        route_id=hashlib.sha256(route_key.encode()).hexdigest()[:24],
        router_version=policy.router_version,
        market_state_id=market.market_state_id,
        paper_winner=winner,
        shadow_alternatives=shadows,
        rejected_families=rejected,
        reason_codes=tuple(dict.fromkeys((*hard_reasons, *soft_reasons))),
        failed_gate_ids=tuple(dict.fromkeys(failed_gates)),
        winner_score=winner_score,
        score_gap=score_gap,
        forced_choice=forced,
        paper_tenor=tenor,
    )
    opportunities = tuple(
        RoutedOpportunity(
            strategy_id=name,
            execution=name == winner,
            bound=item,
            setup_features=_with_route_alternatives(
                item.setup_features,
                tuple(other for other in effective if other != name),
            ),
        )
        for name, item in effective.items()
        if item.binding.eligible
    )
    return decision, opportunities


def _gate_reasons(
    market: MarketState,
    policy: IdentificationPolicy,
    *,
    entries_permitted: bool,
    existing_correlated_exposure: bool,
    cooldown_active: bool,
) -> tuple[list[ReasonCode], list[ReasonCode]]:
    """Live freezes stay hard; paper fail-fast records the same codes as soft."""
    reasons: list[ReasonCode] = []
    if not entries_permitted:
        reasons.append(ReasonCode.ENTRY_FROZEN)
    if existing_correlated_exposure:
        reasons.append(ReasonCode.CORRELATION_LIMIT)
    if cooldown_active:
        reasons.append(ReasonCode.SETUP_COOLDOWN)
    if not market.warmup_complete:
        reasons.append(ReasonCode.WARMUP_INCOMPLETE)
    if market.trend not in {TrendState.UP, TrendState.DOWN}:
        reasons.append(ReasonCode.DATA_GAP)
    if market.iv_percentile is None or market.iv_rv_ratio is None:
        reasons.append(ReasonCode.WARMUP_INCOMPLETE)
    if market.macro_status is MacroStatus.CONFLICT:
        reasons.append(ReasonCode.EVENT_BLACKOUT)
    if market.event_state not in {"NORMAL", "CAUTION"}:
        reasons.append(ReasonCode.EVENT_BLACKOUT)
    if policy.router.paper_fail_fast:
        return [], reasons
    return reasons, []


def _preferred(
    market: MarketState,
    policy: IdentificationPolicy,
    families: frozenset[str],
    *,
    session: SessionBucket,
) -> str | None:
    if policy.router.paper_fail_fast:
        return _paper_preferred(market, policy, families, session=session)
    if market.iv_percentile is None or market.iv_rv_ratio is None:
        return None
    if (
        market.iv_percentile <= policy.router.low_iv_percentile
        and market.iv_rv_ratio <= policy.router.low_iv_rv_ratio
    ):
        return "positional_long_option"
    return "debit_spread"


def _paper_preferred(
    market: MarketState,
    policy: IdentificationPolicy,
    families: frozenset[str],
    *,
    session: SessionBucket,
) -> str | None:
    if "cas_microstructure" in families and session is SessionBucket.AUCTION:
        return "cas_microstructure"
    if market.trend in {TrendState.RANGE, TrendState.MIXED, TrendState.UNKNOWN}:
        return _first_available(
            families,
            ("defined_risk_multileg", "debit_spread", "positional_long_option"),
        )
    pick = "positional_long_option"
    if (
        market.iv_percentile is not None
        and market.iv_rv_ratio is not None
        and (
            market.iv_percentile > policy.router.low_iv_percentile
            or market.iv_rv_ratio > policy.router.low_iv_rv_ratio
        )
    ):
        pick = "debit_spread"
    if pick in families:
        return pick
    return _first_available(families, _FAMILY_ORDER)


def _first_available(families: frozenset[str], order: tuple[str, ...]) -> str | None:
    for name in order:
        if name in families:
            return name
    return next(iter(sorted(families)), None)


def _pick_winner(
    *,
    preferred: str | None,
    families: frozenset[str],
    effective: dict[str, BoundCandidates],
    scored: list[tuple[str, Decimal]],
    policy: IdentificationPolicy,
    hard_reasons: list[ReasonCode],
) -> tuple[str | None, Decimal | None, Decimal | None, list[str], bool]:
    extra: list[str] = []
    if hard_reasons:
        return None, None, None, extra, False

    fail_fast = policy.router.paper_fail_fast
    chosen: str | None = None
    if preferred is None:
        extra.append("preferred_unavailable")
    elif preferred not in families:
        extra.append("preferred_family_blocked")
    elif preferred not in effective or not effective[preferred].binding.eligible:
        extra.append("preferred_ineligible")
    else:
        chosen = preferred

    if chosen is None and fail_fast and scored:
        chosen = scored[0][0]
    if chosen is None:
        return None, None, None, extra, False

    winner_score = effective[chosen].binding.score
    alternatives = [score for name, score in scored if name != chosen]
    score_gap = (
        winner_score
        if not alternatives
        else max(Decimal(0), winner_score - alternatives[0])
    )
    below_floor = winner_score < policy.router.min_winner_score
    thin_gap = score_gap < policy.router.min_score_gap
    if below_floor:
        extra.append("min_winner_score")
    if thin_gap:
        extra.append("min_score_gap")
    if not fail_fast and (below_floor or thin_gap):
        return None, winner_score, score_gap, extra, False
    forced = fail_fast and (chosen != preferred or below_floor or thin_gap)
    return chosen, winner_score, score_gap, extra, forced


def _effective_candidates(
    supplied: dict[str, BoundCandidates],
    families: frozenset[str],
    policy: IdentificationPolicy,
) -> dict[str, BoundCandidates]:
    out = dict(supplied)
    if not policy.router.paper_fail_fast:
        return out
    for name in families:
        item = out.get(name)
        if item is not None and item.binding.eligible:
            continue
        out[name] = _regime_stub(name, policy)
    return out


def _regime_stub(name: str, policy: IdentificationPolicy) -> BoundCandidates:
    return BoundCandidates(
        binding=CandidateBinding(
            strategy_id=name,
            binding_version=policy.binding_version,
            selected_symbols=(),
            score=_prior_score(name, policy),
            eligible=True,
        ),
        candidates=(),
        setup_features=None,
    )


def _prior_score(name: str, policy: IdentificationPolicy) -> Decimal:
    bump = Decimal(0)
    if name == "cas_microstructure":
        bump = _CAS_PRIOR_BUMP
    elif name == "defined_risk_multileg":
        bump = _MULTILEG_PRIOR_BUMP
    return min(_ONE, policy.router.min_winner_score + bump)


def _paper_tenor(
    winner: str | None,
    bound: BoundCandidates | None,
    policy: IdentificationPolicy,
) -> PaperTenor | None:
    if winner is None:
        return None
    if winner in {"cas_microstructure", "defined_risk_multileg"}:
        return PaperTenor.WEEKLY
    dte = (
        None
        if bound is None or bound.setup_features is None
        else bound.setup_features.dte
    )
    if dte is not None:
        if policy.contracts.weekly_dte_min <= dte <= policy.contracts.weekly_dte_max:
            return PaperTenor.WEEKLY
        if policy.contracts.monthly_dte_min <= dte <= policy.contracts.monthly_dte_max:
            return PaperTenor.POSITIONAL
    if winner == "positional_long_option":
        return PaperTenor.POSITIONAL
    return PaperTenor.WEEKLY


def _with_route_alternatives(
    setup: SetupFeatures | None, alternatives: tuple[str, ...]
) -> SetupFeatures | None:
    if setup is None:
        return None
    return setup.model_copy(
        update={"rejected_alternatives": (*setup.rejected_alternatives, *alternatives)}
    )
