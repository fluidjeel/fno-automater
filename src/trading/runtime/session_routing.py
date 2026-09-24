"""Four-mode session routing: mode/family stances and producer dispatch."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from trading.domain.contracts.mode_policy import ModePolicy, ModesConfig
from trading.domain.enums import ExecutionMode, FamilyId, ModeId
from trading.domain.family_gates import (
    CALENDAR_FAMILIES,
    G1_EXCEEDS_BUDGET_FAMILIES,
    G2_UNPROVEN_FAMILIES,
)
from trading.identification.binders import BoundCandidates

if TYPE_CHECKING:
    from trading.domain.contracts import MarketState
    from trading.identification.config import IdentificationPolicy
    from trading.identification.p1_features import ObservedP1Features


class SessionRoutingProfile(StrEnum):
    LEGACY = "legacy"
    FOUR_MODE = "four_mode"


@dataclass(frozen=True, slots=True)
class FamilyProducerSpec:
    """One executable family within a mode."""

    mode_id: ModeId
    family_id: FamilyId
    strategy_id: str
    binder: Callable[..., BoundCandidates]


@dataclass(frozen=True, slots=True)
class ProducedFamilyRequest:
    """Binding output before PaperStrategyRequest assembly."""

    spec: FamilyProducerSpec
    bound: BoundCandidates
    execute: bool
    execution_mode: ExecutionMode


def effective_stance(
    *,
    mode_id: ModeId,
    family_id: FamilyId,
    mode_stances: Mapping[str, ExecutionMode],
    family_stances: Mapping[str, ExecutionMode],
) -> ExecutionMode:
    """Family override wins, then mode stance, else SUSPENDED."""
    family_key = family_id.value
    if family_key in family_stances:
        return family_stances[family_key]
    mode_key = mode_id.value
    return mode_stances.get(mode_key, ExecutionMode.SUSPENDED)


def family_executable(
    stance: ExecutionMode,
    *,
    family_id: FamilyId,
) -> bool:
    """Return whether a family stance permits PAPER submission."""
    if stance is ExecutionMode.SUSPENDED:
        return False
    if family_id.value in G1_EXCEEDS_BUDGET_FAMILIES:
        return False
    if family_id.value in CALENDAR_FAMILIES:
        return False
    if stance is ExecutionMode.PAPER and family_id.value in G2_UNPROVEN_FAMILIES:
        return False
    return stance is ExecutionMode.PAPER


def iter_mode_families(
    modes_config: ModesConfig,
    *,
    mode_stances: Mapping[str, ExecutionMode],
    family_stances: Mapping[str, ExecutionMode],
) -> list[tuple[ModePolicy, FamilyId, ExecutionMode]]:
    """Expand configured stances into routable (mode, family, stance) rows."""
    rows: list[tuple[ModePolicy, FamilyId, ExecutionMode]] = []
    for mode_id, policy in modes_config.modes.items():
        for family in policy.allowed_families:
            stance = effective_stance(
                mode_id=mode_id,
                family_id=family,
                mode_stances=mode_stances,
                family_stances=family_stances,
            )
            if stance is ExecutionMode.SUSPENDED:
                continue
            rows.append((policy, family, stance))
    return rows


def bind_family(
    spec: FamilyProducerSpec,
    candidates: Sequence[object],
    *,
    market: MarketState,
    policy: IdentificationPolicy,
    p1: ObservedP1Features | None,
    master_symbols: frozenset[str] | None,
) -> BoundCandidates:
    """Invoke the family binder with mode-specific kwargs."""
    kwargs: dict[str, object] = {
        "market": market,
        "policy": policy,
        "p1": p1,
    }
    if spec.mode_id in {ModeId.M1_CAS, ModeId.M2_DIRECTIONAL}:
        kwargs["master_symbols"] = master_symbols
    if spec.mode_id is ModeId.M1_CAS:
        return spec.binder(candidates, **kwargs)
    if spec.mode_id is ModeId.M2_DIRECTIONAL:
        return spec.binder(candidates, allow_fallback_expiry=True, **kwargs)
    if spec.family_id in {FamilyId.bull_put_credit, FamilyId.bear_call_credit}:
        return spec.binder(
            candidates,
            family_id=spec.family_id,
            **kwargs,
        )
    return spec.binder(candidates, **kwargs)
