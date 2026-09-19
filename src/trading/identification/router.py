"""Deterministic one-winner NIFTY option structure router."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from trading.domain.contracts import MarketState, RouteDecision
from trading.domain.contracts.identification import (
    MacroStatus,
    SetupFeatures,
    TrendState,
)
from trading.domain.enums import ReasonCode
from trading.identification.binders import BoundCandidates
from trading.identification.config import IdentificationPolicy

__all__ = ["RoutedOpportunity", "route_nifty_options"]


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
) -> tuple[RouteDecision, tuple[RoutedOpportunity, ...]]:
    families = allowed_families or frozenset({"positional_long_option", "debit_spread"})
    candidates = {
        "positional_long_option": long_option,
        "debit_spread": debit_spread,
    }
    hard_reasons: list[ReasonCode] = []
    if not entries_permitted:
        hard_reasons.append(ReasonCode.ENTRY_FROZEN)
    if existing_correlated_exposure:
        hard_reasons.append(ReasonCode.CORRELATION_LIMIT)
    if cooldown_active:
        hard_reasons.append(ReasonCode.SETUP_COOLDOWN)
    if not market.warmup_complete:
        hard_reasons.append(ReasonCode.WARMUP_INCOMPLETE)
    if market.trend not in {TrendState.UP, TrendState.DOWN}:
        hard_reasons.append(ReasonCode.DATA_GAP)
    if market.iv_percentile is None or market.iv_rv_ratio is None:
        hard_reasons.append(ReasonCode.WARMUP_INCOMPLETE)
    if market.macro_status is MacroStatus.CONFLICT:
        hard_reasons.append(ReasonCode.EVENT_BLACKOUT)
    if market.event_state not in {"NORMAL", "CAUTION"}:
        hard_reasons.append(ReasonCode.EVENT_BLACKOUT)

    preferred = _preferred(market, policy)
    if preferred is not None and preferred not in families:
        hard_reasons.append(ReasonCode.STRATEGY_HALTED)
    scored = sorted(
        (
            (name, item.binding.score)
            for name, item in candidates.items()
            if item.binding.eligible and name in families
        ),
        key=lambda pair: (-pair[1], pair[0]),
    )
    winner = None
    winner_score = None
    score_gap = None
    if not hard_reasons and preferred is not None and preferred in families:
        preferred_item = candidates[preferred]
        if preferred_item.binding.eligible:
            winner_score = preferred_item.binding.score
            alternatives = [score for name, score in scored if name != preferred]
            score_gap = (
                winner_score
                if not alternatives
                else max(Decimal(0), winner_score - alternatives[0])
            )
            if (
                winner_score >= policy.router.min_winner_score
                and score_gap >= policy.router.min_score_gap
            ):
                winner = preferred

    shadows = tuple(name for name, _score in scored if name != winner)
    rejected = tuple(
        sorted(
            name
            for name in candidates
            if name not in families or not candidates[name].binding.eligible
        )
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
        reason_codes=tuple(dict.fromkeys(hard_reasons)),
        winner_score=winner_score,
        score_gap=score_gap,
    )
    opportunities = tuple(
        RoutedOpportunity(
            strategy_id=name,
            execution=name == winner,
            bound=item,
            setup_features=_with_route_alternatives(
                item.setup_features,
                tuple(other for other in candidates if other != name),
            ),
        )
        for name, item in candidates.items()
        if item.binding.eligible
    )
    return decision, opportunities


def _preferred(market: MarketState, policy: IdentificationPolicy) -> str | None:
    if market.iv_percentile is None or market.iv_rv_ratio is None:
        return None
    if (
        market.iv_percentile <= policy.router.low_iv_percentile
        and market.iv_rv_ratio <= policy.router.low_iv_rv_ratio
    ):
        return "positional_long_option"
    return "debit_spread"


def _with_route_alternatives(
    setup: SetupFeatures | None, alternatives: tuple[str, ...]
) -> SetupFeatures | None:
    if setup is None:
        return None
    return setup.model_copy(
        update={"rejected_alternatives": (*setup.rejected_alternatives, *alternatives)}
    )
