"""Assess the PAPER P0/P1 data contract against decision-time evidence.

Invariant 6: missing, stale, or zero-invalid P0 state blocks new exposure.
P1 absence is recorded; values are never invented.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading.config.discovery import DiscoveryConfig
from trading.domain.contracts.instrument import InstrumentSpec
from trading.domain.contracts.paper_data import (
    PaperDataAssessment,
    PaperDataField,
    PaperDataFieldResult,
    PaperDataFieldSpec,
    PaperDataPresence,
    PaperDataRequirements,
    PaperDataTier,
)
from trading.domain.contracts.portfolio import PortfolioSnapshot
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import EntryProfile, InstrumentKind, ReasonCode
from trading.domain.primitives import Money
from trading.news.contracts import EventRiskState, NewsQuality
from trading.risk.gate_profile import is_soft

__all__ = ["PaperDataInputs", "assess_paper_data", "strict_would_block_p0"]

_ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class PaperDataInputs:
    """Evidence the assessor may inspect. None means the caller has no value."""

    now: datetime
    snapshots: tuple[FeatureSnapshot, ...]
    event_risk: EventRiskState | None
    portfolio: PortfolioSnapshot | None
    broker_state_ok: bool
    margin_confirmed: bool | None
    margin_required: Money | None
    instruments: Mapping[str, InstrumentSpec] | None = None
    skip: frozenset[PaperDataField] = frozenset()


def assess_paper_data(
    requirements: PaperDataRequirements,
    inputs: PaperDataInputs,
    *,
    entry_profile: EntryProfile = EntryProfile.STRICT,
    discovery_config: DiscoveryConfig | None = None,
    p1_present: frozenset[PaperDataField] = frozenset(),
    p1_absent: frozenset[PaperDataField] = frozenset(),
) -> PaperDataAssessment:
    """Return a complete P0/P1 audit. P1 presence is supplied, never inferred here."""
    results: list[PaperDataFieldResult] = []
    for spec in requirements.fields:
        if spec.field in inputs.skip:
            results.append(
                PaperDataFieldResult(
                    field=spec.field,
                    tier=spec.tier,
                    presence=PaperDataPresence.SKIPPED,
                )
            )
            continue
        if spec.tier is PaperDataTier.P0:
            results.append(_assess_p0(spec, inputs))
            continue
        if spec.field in p1_present:
            results.append(
                PaperDataFieldResult(
                    field=spec.field,
                    tier=spec.tier,
                    presence=PaperDataPresence.PRESENT,
                )
            )
            continue
        if spec.field in p1_absent:
            results.append(
                PaperDataFieldResult(
                    field=spec.field,
                    tier=spec.tier,
                    presence=PaperDataPresence.MISSING,
                    reason_code=ReasonCode.DATA_GAP,
                    detail="p1_absent_not_invented",
                )
            )
            continue
        results.append(
            PaperDataFieldResult(
                field=spec.field,
                tier=spec.tier,
                presence=PaperDataPresence.MISSING,
                reason_code=ReasonCode.DATA_GAP,
                detail="p1_not_observed",
            )
        )
    p0_ok = all(
        _p0_row_ok(row, entry_profile=entry_profile, discovery_config=discovery_config)
        for row in results
        if row.tier is PaperDataTier.P0
    )
    return PaperDataAssessment(
        requirements_version=requirements.requirements_version,
        p0_ok=p0_ok,
        results=tuple(results),
    )


def _p0_row_ok(
    row: PaperDataFieldResult,
    *,
    entry_profile: EntryProfile,
    discovery_config: DiscoveryConfig | None,
) -> bool:
    if row.presence in {PaperDataPresence.PRESENT, PaperDataPresence.SKIPPED}:
        return True
    if row.reason_code is None:
        return False
    return is_soft(row.reason_code, entry_profile, discovery_config)


def strict_would_block_p0(
    assessment: PaperDataAssessment,
    *,
    entry_profile: EntryProfile,
    discovery_config: DiscoveryConfig | None,
) -> tuple[ReasonCode, ...]:
    """P0 failures that are soft under DISCOVERY."""
    codes = [
        row.reason_code
        for row in assessment.results
        if row.tier is PaperDataTier.P0
        and row.reason_code is not None
        and not _p0_row_ok(
            row, entry_profile=entry_profile, discovery_config=discovery_config
        )
        and is_soft(row.reason_code, entry_profile, discovery_config)
    ]
    return tuple(dict.fromkeys(codes))


def _assess_p0(
    spec: PaperDataFieldSpec, inputs: PaperDataInputs
) -> PaperDataFieldResult:
    checker = _P0_CHECKERS[spec.field]
    presence, reason, detail = checker(spec, inputs)
    return PaperDataFieldResult(
        field=spec.field,
        tier=PaperDataTier.P0,
        presence=presence,
        reason_code=reason,
        detail=detail,
    )


def _ok() -> tuple[PaperDataPresence, ReasonCode | None, str | None]:
    return PaperDataPresence.PRESENT, None, None


def _missing(
    reason: ReasonCode, detail: str
) -> tuple[PaperDataPresence, ReasonCode | None, str | None]:
    return PaperDataPresence.MISSING, reason, detail


def _stale(
    detail: str,
) -> tuple[PaperDataPresence, ReasonCode | None, str | None]:
    return PaperDataPresence.STALE, ReasonCode.DATA_STALE, detail


def _zero(
    reason: ReasonCode, detail: str
) -> tuple[PaperDataPresence, ReasonCode | None, str | None]:
    return PaperDataPresence.ZERO_INVALID, reason, detail


def _check_ltp(
    spec: PaperDataFieldSpec, inputs: PaperDataInputs
) -> tuple[PaperDataPresence, ReasonCode | None, str | None]:
    if not inputs.snapshots:
        return _missing(ReasonCode.PRICE_UNAVAILABLE, "no_snapshots")
    for snapshot in inputs.snapshots:
        last = snapshot.market.last
        if last is None:
            return _missing(ReasonCode.PRICE_UNAVAILABLE, snapshot.snapshot_id)
        if spec.zero_invalid and last.value <= 0:
            return _zero(ReasonCode.PRICE_UNAVAILABLE, snapshot.snapshot_id)
    return _check_snapshot_age(spec, inputs.snapshots, inputs.now)


def _check_bid_ask(
    spec: PaperDataFieldSpec, inputs: PaperDataInputs
) -> tuple[PaperDataPresence, ReasonCode | None, str | None]:
    if not inputs.snapshots:
        return _missing(ReasonCode.PRICE_UNAVAILABLE, "no_snapshots")
    for snapshot in inputs.snapshots:
        bid, ask = snapshot.market.bid, snapshot.market.ask
        if bid is None or ask is None:
            return _missing(ReasonCode.PRICE_UNAVAILABLE, snapshot.snapshot_id)
        if spec.zero_invalid and (bid.value <= 0 or ask.value <= 0):
            return _zero(ReasonCode.PRICE_UNAVAILABLE, snapshot.snapshot_id)
    return _check_snapshot_age(spec, inputs.snapshots, inputs.now)


def _check_freshness(
    spec: PaperDataFieldSpec, inputs: PaperDataInputs
) -> tuple[PaperDataPresence, ReasonCode | None, str | None]:
    if not inputs.snapshots:
        return _missing(ReasonCode.DATA_STALE, "no_snapshots")
    for snapshot in inputs.snapshots:
        if snapshot.times.event_time > inputs.now:
            return _missing(ReasonCode.SNAPSHOT_MISMATCH, snapshot.snapshot_id)
        if snapshot.quality.state.blocks_new_exposure:
            return _stale(snapshot.snapshot_id)
    return _check_snapshot_age(spec, inputs.snapshots, inputs.now)


def _check_volume(
    spec: PaperDataFieldSpec, inputs: PaperDataInputs
) -> tuple[PaperDataPresence, ReasonCode | None, str | None]:
    if not inputs.snapshots:
        return _missing(ReasonCode.DATA_GAP, "no_snapshots")
    for snapshot in inputs.snapshots:
        volume = snapshot.market.volume
        if volume is None:
            return _missing(ReasonCode.DATA_GAP, snapshot.snapshot_id)
        if spec.zero_invalid and volume <= 0:
            return _zero(ReasonCode.DATA_INVALID, snapshot.snapshot_id)
    return _ok()


def _check_open_interest(
    spec: PaperDataFieldSpec, inputs: PaperDataInputs
) -> tuple[PaperDataPresence, ReasonCode | None, str | None]:
    derivatives = _derivative_snapshots(inputs.snapshots)
    if not derivatives:
        return _missing(ReasonCode.DEPTH_INSUFFICIENT, "no_derivative_snapshots")
    for snapshot in derivatives:
        context = snapshot.derivatives
        if context is None:
            return _missing(ReasonCode.DEPTH_INSUFFICIENT, snapshot.snapshot_id)
        oi = context.open_interest
        if oi is None:
            return _missing(ReasonCode.DEPTH_INSUFFICIENT, snapshot.snapshot_id)
        if spec.zero_invalid and oi <= 0:
            return _zero(ReasonCode.DEPTH_INSUFFICIENT, snapshot.snapshot_id)
    return _ok()


def _check_metadata(
    spec: PaperDataFieldSpec, inputs: PaperDataInputs
) -> tuple[PaperDataPresence, ReasonCode | None, str | None]:
    del spec
    if not inputs.snapshots:
        return _missing(ReasonCode.INSTRUMENT_UNKNOWN, "no_snapshots")
    for snapshot in inputs.snapshots:
        contract = snapshot.contract
        if not contract.symbol or contract.underlying is None:
            return _missing(ReasonCode.INSTRUMENT_UNKNOWN, snapshot.snapshot_id)
        kind = contract.instrument_kind
        if kind is InstrumentKind.OPTION and (
            contract.expiry is None
            or contract.strike is None
            or contract.option_type is None
        ):
            return _missing(ReasonCode.INSTRUMENT_UNKNOWN, snapshot.snapshot_id)
        if kind is InstrumentKind.FUTURE and contract.expiry is None:
            return _missing(ReasonCode.INSTRUMENT_UNKNOWN, snapshot.snapshot_id)
        if kind in {InstrumentKind.OPTION, InstrumentKind.FUTURE} and not _lot_size_ok(
            snapshot, inputs.instruments
        ):
            return _zero(ReasonCode.INSTRUMENT_UNKNOWN, snapshot.snapshot_id)
    return _ok()


def _check_margin(
    spec: PaperDataFieldSpec, inputs: PaperDataInputs
) -> tuple[PaperDataPresence, ReasonCode | None, str | None]:
    if inputs.margin_confirmed is None or inputs.margin_required is None:
        return _missing(ReasonCode.MARGIN_INSUFFICIENT, "margin_not_previewed")
    if not inputs.margin_confirmed:
        return _missing(ReasonCode.MARGIN_INSUFFICIENT, "margin_unconfirmed")
    if spec.zero_invalid and inputs.margin_required.is_zero:
        return _zero(ReasonCode.MARGIN_INSUFFICIENT, "margin_zero")
    return _ok()


def _check_broker(
    spec: PaperDataFieldSpec, inputs: PaperDataInputs
) -> tuple[PaperDataPresence, ReasonCode | None, str | None]:
    del spec
    if inputs.portfolio is None:
        return _missing(ReasonCode.RECONCILIATION_UNRESOLVED, "portfolio_missing")
    if not inputs.broker_state_ok:
        return _missing(ReasonCode.RECONCILIATION_UNRESOLVED, "broker_state_blocked")
    return _ok()


def _check_event(
    spec: PaperDataFieldSpec, inputs: PaperDataInputs
) -> tuple[PaperDataPresence, ReasonCode | None, str | None]:
    event = inputs.event_risk
    if event is None:
        return _missing(ReasonCode.EVENT_BLACKOUT, "event_state_missing")
    if event.quality_state is not NewsQuality.VALID:
        return _missing(ReasonCode.DATA_INVALID, "event_quality")
    if inputs.now < event.as_of:
        return _missing(ReasonCode.SNAPSHOT_MISMATCH, "event_in_future")
    if inputs.now >= event.expires_at:
        return _stale("event_expired")
    if spec.max_age_ms is not None:
        age_ms = int((inputs.now - event.as_of).total_seconds() * 1000)
        if age_ms > spec.max_age_ms:
            return _stale("event_as_of_stale")
    return _ok()


def _check_snapshot_age(
    spec: PaperDataFieldSpec,
    snapshots: Sequence[FeatureSnapshot],
    now: datetime,
) -> tuple[PaperDataPresence, ReasonCode | None, str | None]:
    if spec.max_age_ms is None:
        return _ok()
    for snapshot in snapshots:
        age = snapshot.times.age_at(now)
        age_ms = int(age.total_seconds() * 1000)
        if age.total_seconds() < 0 or age_ms > spec.max_age_ms:
            return _stale(snapshot.snapshot_id)
    return _ok()


def _derivative_snapshots(
    snapshots: Sequence[FeatureSnapshot],
) -> tuple[FeatureSnapshot, ...]:
    return tuple(
        item
        for item in snapshots
        if item.contract.instrument_kind
        in {InstrumentKind.OPTION, InstrumentKind.FUTURE}
    )


def _lot_size_ok(
    snapshot: FeatureSnapshot, instruments: Mapping[str, InstrumentSpec] | None
) -> bool:
    if instruments is not None:
        spec = instruments.get(snapshot.contract.symbol)
        if spec is not None and spec.lot_size > 0:
            return True
        for item in instruments.values():
            if item.trading_symbol == snapshot.contract.symbol and item.lot_size > 0:
                return True
    lot = snapshot.features.get("lot_size")
    return lot is not None and lot > _ZERO


_P0_CHECKERS = {
    PaperDataField.LTP: _check_ltp,
    PaperDataField.BID_ASK: _check_bid_ask,
    PaperDataField.QUOTE_FRESHNESS: _check_freshness,
    PaperDataField.VOLUME: _check_volume,
    PaperDataField.OPEN_INTEREST: _check_open_interest,
    PaperDataField.CONTRACT_METADATA: _check_metadata,
    PaperDataField.MARGIN_ESTIMATE: _check_margin,
    PaperDataField.POSITION_BROKER_STATE: _check_broker,
    PaperDataField.EVENT_STATE: _check_event,
}
