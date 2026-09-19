"""Multi-leg quote-snapshot coherence for Layer 2.

Leg quotes keep their own snapshot IDs. Copying the parent intent snapshot_id
onto every option leg destroys quote provenance and is rejected as a design.
Coherence is the decision cycle (intent.snapshot_id) plus timestamp skew,
instrument identity, freshness and causality — not ID equality.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from trading.config.schema import ConfigNotVerifiedError, FreshnessRules
from trading.domain.contracts.intent import IntentLeg, TradeIntent
from trading.domain.contracts.risk import LegQuoteRef
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import ReasonCode

__all__ = ["LegSnapshotBundle", "validate_leg_snapshot_bundle"]

_SKEW_COMPARISON_LEGS = 2


@dataclass(frozen=True, slots=True)
class LegSnapshotBundle:
    """Validated per-leg quote evidence attached to one RiskDecision."""

    decision_snapshot_id: str
    decision_timestamp: datetime
    leg_quotes: tuple[LegQuoteRef, ...]
    reason: ReasonCode | None = None


def validate_leg_snapshot_bundle(
    intent: TradeIntent,
    leg_snapshots: Mapping[str, FeatureSnapshot],
    *,
    now: datetime,
    freshness: FreshnessRules,
) -> LegSnapshotBundle:
    """Validate a multi-leg quote map without rewriting snapshot IDs.

    Distinct snapshot IDs are required to stay distinct. Coherence is the
    decision cycle (intent.snapshot_id) plus timestamp skew, not ID equality.
    """
    quotes = _collect_evidence(intent, leg_snapshots)
    audit = LegSnapshotBundle(
        decision_snapshot_id=intent.snapshot_id,
        decision_timestamp=now,
        leg_quotes=quotes,
    )
    if len(leg_snapshots) != len(intent.legs):
        return _reject(audit, ReasonCode.SNAPSHOT_MISMATCH)
    for leg in intent.legs:
        snapshot = leg_snapshots.get(leg.leg_id)
        if snapshot is None:
            return _reject(audit, ReasonCode.SNAPSHOT_MISMATCH)
        identity = _instrument_identity_reason(leg, snapshot)
        if identity is not None:
            return _reject(audit, identity)
    try:
        max_age_ms = freshness.require_quote_max_age_ms()
        max_skew_ms = freshness.require_max_leg_quote_skew_ms()
    except ConfigNotVerifiedError:
        return _reject(audit, ReasonCode.CONFIG_UNVERIFIED)

    event_times: list[datetime] = []
    for snapshot in (leg_snapshots[leg.leg_id] for leg in intent.legs):
        if snapshot.times.event_time > now:
            return _reject(audit, ReasonCode.SNAPSHOT_MISMATCH)
        if snapshot.quality.state.blocks_new_exposure:
            return _reject(audit, ReasonCode.DATA_STALE)
        age = snapshot.times.age_at(now)
        if age < timedelta(0) or _timedelta_ms(age) > max_age_ms:
            return _reject(audit, ReasonCode.DATA_STALE)
        event_times.append(snapshot.times.event_time)
    if len(event_times) >= _SKEW_COMPARISON_LEGS:
        skew = max(event_times) - min(event_times)
        if _timedelta_ms(skew) > max_skew_ms:
            return _reject(audit, ReasonCode.SNAPSHOT_MISMATCH)
    return audit


def _collect_evidence(
    intent: TradeIntent,
    leg_snapshots: Mapping[str, FeatureSnapshot],
) -> tuple[LegQuoteRef, ...]:
    refs: list[LegQuoteRef] = []
    for leg in intent.legs:
        snapshot = leg_snapshots.get(leg.leg_id)
        if snapshot is None:
            continue
        refs.append(
            LegQuoteRef(
                leg_id=leg.leg_id,
                snapshot_id=snapshot.snapshot_id,
                symbol=snapshot.contract.symbol,
                event_time=snapshot.times.event_time,
                calculation_time=snapshot.times.calculation_time,
            )
        )
    return tuple(refs)


def _instrument_identity_reason(
    leg: IntentLeg, snapshot: FeatureSnapshot
) -> ReasonCode | None:
    contract = snapshot.contract
    expected = leg.contract
    if (
        contract.symbol != expected.symbol
        or contract.instrument_kind != expected.instrument_kind
        or contract.underlying != expected.underlying
        or contract.expiry != expected.expiry
        or contract.strike != expected.strike
        or contract.option_type != expected.option_type
    ):
        return ReasonCode.SNAPSHOT_MISMATCH
    return None


def _reject(audit: LegSnapshotBundle, reason: ReasonCode) -> LegSnapshotBundle:
    return LegSnapshotBundle(
        decision_snapshot_id=audit.decision_snapshot_id,
        decision_timestamp=audit.decision_timestamp,
        leg_quotes=audit.leg_quotes,
        reason=reason,
    )


def _timedelta_ms(delta: timedelta) -> int:
    return int(delta.total_seconds() * 1000)
