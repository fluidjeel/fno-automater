"""Build PaperStrategyRequest rows from four-mode family producers."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import cast

from trading.config.discovery import DiscoveryConfig
from trading.domain.contracts import FeatureSnapshot, InstrumentSpec, MarketState
from trading.domain.contracts.mode_policy import ModesConfig
from trading.domain.enums import EntryProfile, ExecutionMode, FamilyId, ModeId
from trading.domain.family_gates import effective_family_stances
from trading.identification import (
    bind_credit_spread,
    bind_debit_spread,
    bind_iron_condor,
    bind_long_call_butterfly,
    bind_long_put_butterfly,
    bind_long_straddle,
    bind_long_strangle,
    bind_m1_cas_option,
    bind_m2_long_option,
    bind_short_iron_butterfly,
)
from trading.identification.binders import BoundCandidates
from trading.identification.config import IdentificationPolicy
from trading.identification.p1_features import ObservedP1Features
from trading.news.contracts import EventRiskState
from trading.runtime.cohort import experiment_id_for
from trading.runtime.paper_runner import PaperStrategyRequest
from trading.runtime.session_routing import (
    FamilyProducerSpec,
    ProducedFamilyRequest,
    bind_family,
    family_executable,
    iter_mode_families,
)
from trading.strategies.macro import MacroAssessment

__all__ = [
    "FAMILY_PRODUCER_REGISTRY",
    "build_four_mode_requests",
    "iter_recordable_family_slots",
    "producer_spec_for",
]

# (mode, family) -> (strategy_id, binder). Calendars intentionally omitted.
FAMILY_PRODUCER_REGISTRY: dict[tuple[ModeId, FamilyId], tuple[str, object]] = {
    (ModeId.M1_CAS, FamilyId.long_call): (
        "cas_microstructure",
        bind_m1_cas_option,
    ),
    (ModeId.M1_CAS, FamilyId.long_put): (
        "cas_microstructure",
        bind_m1_cas_option,
    ),
    (ModeId.M2_DIRECTIONAL, FamilyId.long_call): (
        "positional_long_option",
        bind_m2_long_option,
    ),
    (ModeId.M2_DIRECTIONAL, FamilyId.long_put): (
        "positional_long_option",
        bind_m2_long_option,
    ),
    (ModeId.M3_TACTICAL_POSITIONAL, FamilyId.bull_call_debit): (
        "debit_spread",
        bind_debit_spread,
    ),
    (ModeId.M3_TACTICAL_POSITIONAL, FamilyId.bear_put_debit): (
        "debit_spread",
        bind_debit_spread,
    ),
    (ModeId.M3_TACTICAL_POSITIONAL, FamilyId.bull_put_credit): (
        "bull_put_credit",
        bind_credit_spread,
    ),
    (ModeId.M3_TACTICAL_POSITIONAL, FamilyId.bear_call_credit): (
        "bear_call_credit",
        bind_credit_spread,
    ),
    (ModeId.M4_STRATEGIC_POSITIONAL, FamilyId.bull_call_debit): (
        "debit_spread",
        bind_debit_spread,
    ),
    (ModeId.M4_STRATEGIC_POSITIONAL, FamilyId.bear_put_debit): (
        "debit_spread",
        bind_debit_spread,
    ),
    (ModeId.M4_STRATEGIC_POSITIONAL, FamilyId.bull_put_credit): (
        "bull_put_credit",
        bind_credit_spread,
    ),
    (ModeId.M4_STRATEGIC_POSITIONAL, FamilyId.bear_call_credit): (
        "bear_call_credit",
        bind_credit_spread,
    ),
    (ModeId.M4_STRATEGIC_POSITIONAL, FamilyId.short_iron_condor_defined): (
        "iron_condor",
        bind_iron_condor,
    ),
    (ModeId.M4_STRATEGIC_POSITIONAL, FamilyId.short_iron_butterfly_defined): (
        FamilyId.short_iron_butterfly_defined.value,
        bind_short_iron_butterfly,
    ),
    (ModeId.M4_STRATEGIC_POSITIONAL, FamilyId.long_call_butterfly): (
        FamilyId.long_call_butterfly.value,
        bind_long_call_butterfly,
    ),
    (ModeId.M4_STRATEGIC_POSITIONAL, FamilyId.long_put_butterfly): (
        FamilyId.long_put_butterfly.value,
        bind_long_put_butterfly,
    ),
    (ModeId.M4_STRATEGIC_POSITIONAL, FamilyId.long_straddle): (
        FamilyId.long_straddle.value,
        bind_long_straddle,
    ),
    (ModeId.M4_STRATEGIC_POSITIONAL, FamilyId.long_strangle): (
        FamilyId.long_strangle.value,
        bind_long_strangle,
    ),
}


def iter_recordable_family_slots(
    modes_config: ModesConfig,
    *,
    mode_stances: Mapping[str, ExecutionMode],
    family_stances: Mapping[str, ExecutionMode],
    entry_profile: EntryProfile = EntryProfile.STRICT,
    discovery_config: DiscoveryConfig | None = None,
) -> tuple[tuple[ModeId, FamilyId], ...]:
    """Expand routable (mode, family) pairs that have a registered producer."""
    merged_family_stances = effective_family_stances(
        family_stances,
        entry_profile=entry_profile,
        discovery_family_stances=(
            discovery_config.family_stances if discovery_config is not None else None
        ),
    )
    slots: list[tuple[ModeId, FamilyId]] = []
    for policy_row, family_id, _stance in iter_mode_families(
        modes_config,
        mode_stances=mode_stances,
        family_stances=merged_family_stances,
    ):
        if producer_spec_for(policy_row.mode_id, family_id) is None:
            continue
        slots.append((policy_row.mode_id, family_id))
    return tuple(slots)


def producer_spec_for(
    mode_id: ModeId, family_id: FamilyId
) -> FamilyProducerSpec | None:
    """Resolve producer metadata for one mode/family pair."""
    entry = FAMILY_PRODUCER_REGISTRY.get((mode_id, family_id))
    if entry is None:
        return None
    strategy_id, binder = entry
    return FamilyProducerSpec(
        mode_id=mode_id,
        family_id=family_id,
        strategy_id=strategy_id,
        binder=cast(Callable[..., BoundCandidates], binder),
    )


def produce_family_requests(
    *,
    modes_config: ModesConfig,
    mode_stances: Mapping[str, ExecutionMode],
    family_stances: Mapping[str, ExecutionMode],
    candidates: Sequence[FeatureSnapshot],
    market: MarketState,
    policy: IdentificationPolicy,
    p1: ObservedP1Features | None,
    master_symbols: frozenset[str] | None,
    discovery_config: DiscoveryConfig | None = None,
    entry_profile: EntryProfile = EntryProfile.STRICT,
) -> tuple[ProducedFamilyRequest, ...]:
    """Evaluate every configured family producer without legacy router gating."""
    merged_family_stances = effective_family_stances(
        family_stances,
        entry_profile=entry_profile,
        discovery_family_stances=(
            discovery_config.family_stances if discovery_config is not None else None
        ),
    )
    produced: list[ProducedFamilyRequest] = []
    for policy_row, family_id, stance in iter_mode_families(
        modes_config,
        mode_stances=mode_stances,
        family_stances=merged_family_stances,
    ):
        spec = producer_spec_for(policy_row.mode_id, family_id)
        if spec is None:
            continue
        bound = bind_family(
            spec,
            _candidates_for_mode(candidates, mode_id=policy_row.mode_id),
            market=market,
            policy=policy,
            p1=p1,
            master_symbols=master_symbols,
            discovery_config=discovery_config,
            entry_profile=entry_profile,
        )
        execute = (
            family_executable(
                stance,
                family_id=family_id,
                entry_profile=entry_profile,
            )
            and bound.binding.eligible
        )
        mode = ExecutionMode.PAPER if execute else ExecutionMode.SHADOW
        produced.append(
            ProducedFamilyRequest(
                spec=spec,
                bound=bound,
                execute=execute,
                execution_mode=mode,
            )
        )
    return tuple(produced)


def build_four_mode_requests(
    produced: Sequence[ProducedFamilyRequest],
    *,
    index_underlying: FeatureSnapshot,
    instruments: Mapping[str, InstrumentSpec],
    event_risk: EventRiskState | None,
    macro: MacroAssessment | None,
    experiment_prefix: str,
    now: datetime,
    discovery_fingerprint: str | None = None,
) -> tuple[PaperStrategyRequest, ...]:
    """Map producer outputs to PaperStrategyRequest rows for PaperRunner."""
    requests: list[PaperStrategyRequest] = []
    for item in produced:
        strategy_key = f"{item.spec.mode_id.value}:{item.spec.family_id.value}"
        requests.append(
            PaperStrategyRequest(
                strategy_id=item.spec.strategy_id,
                underlying=index_underlying,
                candidates=item.bound.candidates,
                instruments=instruments,
                event_risk_state=event_risk,
                experiment_id=experiment_id_for(
                    experiment_prefix,
                    strategy_key,
                    now,
                    discovery_fingerprint=discovery_fingerprint,
                ),
                execution_mode=item.execution_mode,
                macro=macro,
                execute=item.execute,
                setup_features=item.bound.setup_features,
                route_decision=None,
                forced_mode_id=item.spec.mode_id,
                forced_family_id=item.spec.family_id,
                binding_reason_codes=item.bound.binding.reason_codes,
            )
        )
    return tuple(requests)


def _candidates_for_mode(
    candidates: Sequence[FeatureSnapshot], *, mode_id: ModeId
) -> tuple[FeatureSnapshot, ...]:
    """Filter the merged chain per mode.

    Following-week rows carry ≥7-DTE expiries when the loaded chain is nearer;
    M2, M3 and M4 all need them. M1 CAS trades the near chain only.
    """
    if mode_id is ModeId.M1_CAS:
        return tuple(
            item
            for item in candidates
            if item.features.get("following_week_chain", Decimal(0)) != Decimal(1)
        )
    return tuple(candidates)
