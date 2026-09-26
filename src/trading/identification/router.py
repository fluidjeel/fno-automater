"""Deterministic one-winner NIFTY option structure router."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from trading.domain.contracts import MarketState, RouteDecision
from trading.domain.contracts.identification import (
    MacroStatus,
    SetupFeatures,
    TrendState,
    VolatilityState,
)
from trading.domain.contracts.paper_data import PaperDataField
from trading.domain.enums import EntryProfile, ReasonCode
from trading.identification.allow_table import allowed_families_for
from trading.identification.binders import BoundCandidates
from trading.identification.config import IdentificationPolicy
from trading.identification.p1_features import ObservedP1Features
from trading.risk.gate_profile import is_soft

if TYPE_CHECKING:
    from trading.config.discovery import DiscoveryConfig

__all__ = ["RoutedOpportunity", "route_nifty_options"]


@dataclass(frozen=True, slots=True)
class RoutedOpportunity:
    strategy_id: str
    execution: bool
    bound: BoundCandidates
    setup_features: SetupFeatures | None


def route_nifty_options(  # noqa: PLR0912, PLR0915 - fail-closed winner gates
    market: MarketState,
    *,
    long_option: BoundCandidates,
    debit_spread: BoundCandidates,
    policy: IdentificationPolicy,
    entries_permitted: bool = True,
    existing_correlated_exposure: bool = False,
    cooldown_active: bool = False,
    allowed_families: frozenset[str] | None = None,
    p1: ObservedP1Features | None = None,
    entry_profile: EntryProfile = EntryProfile.STRICT,
    discovery_config: DiscoveryConfig | None = None,
) -> tuple[RouteDecision, tuple[RoutedOpportunity, ...]]:
    families = (
        allowed_families
        if allowed_families is not None
        else allowed_families_for(market, policy)
    )
    candidates = {
        "positional_long_option": long_option,
        "debit_spread": debit_spread,
    }
    hard_reasons: list[ReasonCode] = []
    soft_reasons: list[ReasonCode] = []
    if not entries_permitted:
        hard_reasons.append(ReasonCode.ENTRY_FROZEN)
    if existing_correlated_exposure:
        hard_reasons.append(ReasonCode.CORRELATION_LIMIT)
    if cooldown_active:
        if is_soft(ReasonCode.SETUP_COOLDOWN, entry_profile, discovery_config):
            soft_reasons.append(ReasonCode.SETUP_COOLDOWN)
        else:
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

    preferred = _preferred(market, policy, p1=p1)
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
            expanding_rv = (
                p1 is not None
                and PaperDataField.REALIZED_VOLATILITY in p1.present
                and market.volatility is VolatilityState.EXPANDING
                and preferred == "debit_spread"
            )
            if winner_score >= policy.router.min_winner_score and (
                expanding_rv or score_gap >= policy.router.min_score_gap
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
    failed_gates: list[str] = [reason.value for reason in hard_reasons]
    if winner is None and not hard_reasons:
        if preferred is None:
            failed_gates.append("preferred_unavailable")
        elif preferred not in families:
            failed_gates.append("preferred_family_blocked")
        elif not candidates[preferred].binding.eligible:
            failed_gates.append("preferred_ineligible")
        elif winner_score is not None and winner_score < policy.router.min_winner_score:
            failed_gates.append("min_winner_score")
        elif score_gap is not None and score_gap < policy.router.min_score_gap:
            failed_gates.append("min_score_gap")
        else:
            failed_gates.append("no_clear_winner")

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
        strict_would_block=tuple(dict.fromkeys(soft_reasons)),
        failed_gate_ids=tuple(dict.fromkeys(failed_gates)),
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


def _preferred(
    market: MarketState,
    policy: IdentificationPolicy,
    *,
    p1: ObservedP1Features | None = None,
) -> str | None:
    if market.iv_percentile is None or market.iv_rv_ratio is None:
        return None
    if (
        p1 is not None
        and PaperDataField.REALIZED_VOLATILITY in p1.present
        and market.volatility is VolatilityState.EXPANDING
    ):
        return "debit_spread"
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
