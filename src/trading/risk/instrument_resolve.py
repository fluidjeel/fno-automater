"""DISCOVERY-only instrument resolution before Layer 2 risk evaluation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from trading.domain.contracts import FeatureSnapshot, InstrumentSpec, TradeIntent
from trading.domain.enums import EntryProfile, InstrumentKind, ReasonCode
from trading.risk.instrument_registry import InstrumentRegistry

__all__ = [
    "DiscoveryRiskContext",
    "resolve_discovery_risk_context",
]


@dataclass(frozen=True, slots=True)
class DiscoveryRiskContext:
    """Resolved instrument evidence for one DISCOVERY risk evaluation."""

    instrument: InstrumentSpec
    instruments: dict[str, InstrumentSpec]
    feature_snapshot: FeatureSnapshot
    leg_snapshots: dict[str, FeatureSnapshot]


def resolve_discovery_risk_context(
    intent: TradeIntent,
    *,
    candidates: tuple[FeatureSnapshot, ...],
    instruments: Mapping[str, InstrumentSpec],
    underlying: FeatureSnapshot,
    registry: InstrumentRegistry,
) -> DiscoveryRiskContext | ReasonCode:
    """Resolve specs and snapshots for one intent before risk evaluation."""
    symbols = frozenset(leg.contract.symbol for leg in intent.legs)
    resolved = dict(instruments)
    ensured = registry.ensure(symbols)
    if len(ensured) != len(symbols):
        return ReasonCode.INSTRUMENT_UNKNOWN
    resolved.update(ensured)

    by_symbol = {snap.contract.symbol: snap for snap in candidates}
    leg_snapshots: dict[str, FeatureSnapshot] = {}
    for leg in intent.legs:
        snap = by_symbol.get(leg.contract.symbol)
        if snap is None:
            return ReasonCode.INSTRUMENT_UNKNOWN
        if snap.derivatives is None and snap.contract.instrument_kind in {
            InstrumentKind.OPTION,
            InstrumentKind.FUTURE,
        }:
            return ReasonCode.INSTRUMENT_UNKNOWN
        leg_snapshots[leg.leg_id] = snap

    primary = intent.legs[0].contract.symbol
    instrument = resolved.get(primary)
    if instrument is None:
        return ReasonCode.INSTRUMENT_UNKNOWN
    feature = leg_snapshots[intent.legs[0].leg_id]
    if feature.snapshot_id != intent.snapshot_id:
        feature = feature.model_copy(update={"snapshot_id": intent.snapshot_id})
    return DiscoveryRiskContext(
        instrument=instrument,
        instruments=resolved,
        feature_snapshot=feature,
        leg_snapshots=leg_snapshots,
    )


def should_resolve_instruments(entry_profile: EntryProfile) -> bool:
    """True when missing catalog rows may be healed before risk evaluation."""
    return entry_profile is EntryProfile.DISCOVERY
