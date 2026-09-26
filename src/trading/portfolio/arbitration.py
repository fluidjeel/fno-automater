"""Portfolio Arbitration: exact duplicates, economic overlap, conflict, M4 cap (P4/P12)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from trading.config.discovery import DiscoveryConfig
from trading.domain.contracts.base import StrictModel
from trading.domain.contracts.intent import IntentLeg, TradeIntent
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.contracts.position import PositionState
from trading.domain.enums import EntryProfile, FamilyId, ModeId, ReasonCode, TradeState
from trading.portfolio.economic_overlap import (
    EconomicExposureKey,
    count_m4_positions,
    directions_conflict,
    economic_keys_overlap,
    extract_economic_exposure,
)

__all__ = [
    "ArbitrationResult",
    "ArbitrationSoftWarning",
    "ArbitrationSuppression",
    "CounterfactualLogEntry",
    "PortfolioArbiter",
    "count_mode_entries_today",
    "extract_leg_signature",
]

_IST = ZoneInfo("Asia/Kolkata")

# Canonical signature type for normalized option structures:
# tuple of ((symbol, side, ratio), ...) sorted alphabetically
_StructureSignature = tuple[tuple[str, str, int], ...]
_IncumbentKey = tuple[object, ...]


def _normalize_intent_legs(legs: Sequence[IntentLeg]) -> _StructureSignature:
    """Derive canonical sorted tuple of (symbol, side, ratio) for intent legs."""
    raw_legs = [(leg.contract.symbol, leg.side.value, leg.ratio) for leg in legs]
    return tuple(sorted(raw_legs, key=lambda x: (x[0], x[1], x[2])))


def _normalize_position_legs(position: PositionState) -> _StructureSignature:
    """Derive canonical sorted tuple of (symbol, side, 1) for open position legs."""
    raw_legs = [(leg.contract.symbol, leg.side.value, 1) for leg in position.legs]
    return tuple(sorted(raw_legs, key=lambda x: (x[0], x[1], x[2])))


def extract_leg_signature(
    intent: TradeIntent,
) -> tuple[str, str, _StructureSignature]:
    """Extract full comparison key: (underlying, expiry_iso, normalized_legs)."""
    underlying = intent.underlying
    expiry_str = (
        intent.legs[0].contract.expiry.isoformat()
        if intent.legs and intent.legs[0].contract.expiry
        else "NONE"
    )
    norm_legs = _normalize_intent_legs(intent.legs)
    return underlying, expiry_str, norm_legs


def count_mode_entries_today(
    lifecycles: Sequence[PositionLifecycleRecord],
    *,
    session_date: date,
    zone: ZoneInfo = _IST,
) -> dict[ModeId, int]:
    """Count distinct trades opened on ``session_date`` per mode."""
    counts: dict[ModeId, int] = {}
    seen: set[str] = set()
    for item in lifecycles:
        if item.trade_id in seen:
            continue
        mode_id = item.mode_id or item.position.mode_id
        opened_at = item.position.opened_at
        if mode_id is None or opened_at is None:
            continue
        if opened_at.astimezone(zone).date() != session_date:
            continue
        seen.add(item.trade_id)
        counts[mode_id] = counts.get(mode_id, 0) + 1
    return counts


@dataclass(frozen=True, slots=True)
class ArbitrationSuppression:
    """Record of an intent suppressed by arbitration with reference to incumbent."""

    candidate_intent_id: str
    candidate_mode_id: ModeId | None
    candidate_family_id: FamilyId | None
    incumbent_id: str
    reason_code: ReasonCode
    detail: str


@dataclass(frozen=True, slots=True)
class ArbitrationSoftWarning:
    """DISCOVERY-only shadow of a rule that would suppress under STRICT."""

    intent_id: str
    reason_code: ReasonCode
    incumbent_id: str | None
    detail: str


class CounterfactualLogEntry(StrictModel):
    """Hypothetical record of candidate evaluation without capital reservation."""

    candidate_intent_id: str
    mode_id: ModeId | None
    family_id: FamilyId | None
    action: str
    incumbent_id: str | None
    timestamp: datetime
    requested_risk_amount: Decimal


class ArbitrationResult(StrictModel):
    """Result of arbitrating multi-mode candidates in one decision cycle."""

    approved_intents: tuple[TradeIntent, ...]
    suppressed_intents: tuple[ArbitrationSuppression, ...]
    counterfactual_log: tuple[CounterfactualLogEntry, ...]
    approved_object_ids: tuple[int, ...] = ()
    suppressed_object_ids: tuple[int, ...] = ()
    soft_warnings: tuple[ArbitrationSoftWarning, ...] = ()


class PortfolioArbiter:
    """Arbitrates candidates from multiple mode producers."""

    def __init__(
        self,
        *,
        max_m4_open_positions: int,
        discovery_config: DiscoveryConfig | None = None,
        entry_profile: EntryProfile = EntryProfile.STRICT,
    ) -> None:
        self._max_m4_open_positions = max_m4_open_positions
        self._discovery_config = discovery_config
        self._entry_profile = entry_profile

    @property
    def _discovery_mode(self) -> bool:
        return (
            self._entry_profile is EntryProfile.DISCOVERY
            and self._discovery_config is not None
        )

    @property
    def _discovery(self) -> DiscoveryConfig:
        """Validated DISCOVERY config; only call when ``_discovery_mode`` is true."""
        if self._discovery_config is None:
            raise RuntimeError("discovery config is required for DISCOVERY arbitration")
        return self._discovery_config

    def arbitrate(
        self,
        candidates: Sequence[TradeIntent],
        *,
        existing_positions: Sequence[PositionState | PositionLifecycleRecord] = (),
        pending_intents: Sequence[TradeIntent] = (),
        now: datetime,
        mode_daily_entries: Mapping[ModeId, int] | None = None,
    ) -> ArbitrationResult:
        """Arbitrate candidate intents with duplicate, overlap, conflict, and cap rules."""
        approved: list[TradeIntent] = []
        approved_objects: list[int] = []
        suppressed: list[ArbitrationSuppression] = []
        suppressed_objects: list[int] = []
        counterfactuals: list[CounterfactualLogEntry] = []
        soft_warnings: list[ArbitrationSoftWarning] = []

        incumbents: dict[_IncumbentKey, str] = {}
        economic_incumbents: dict[EconomicExposureKey, str] = {}
        active_economic_keys: list[tuple[EconomicExposureKey, str]] = []
        mode_entry_counts = dict(mode_daily_entries or {})
        mode_open_counts = _initial_mode_open_counts(
            existing_positions,
            pending_intents=pending_intents,
        )

        for item in existing_positions:
            pos = item.position if isinstance(item, PositionLifecycleRecord) else item
            if pos.state not in {TradeState.OPEN, TradeState.PENDING_ENTRY}:
                continue
            if not pos.legs:
                continue
            mode_id = _position_mode_id(item)
            underlying = pos.legs[0].contract.underlying
            expiry_str = (
                pos.legs[0].contract.expiry.isoformat()
                if pos.legs[0].contract.expiry
                else "NONE"
            )
            sig = (underlying, expiry_str, _normalize_position_legs(pos))
            incumbents[_duplicate_key(sig, mode_id, discovery=self._discovery_mode)] = (
                pos.trade_id
            )
            intent = item.intent if isinstance(item, PositionLifecycleRecord) else None
            if intent is not None:
                econ = extract_economic_exposure(intent)
                if econ is not None:
                    economic_incumbents[econ] = pos.trade_id
                    active_economic_keys.append((econ, pos.trade_id))

        for pending in pending_intents:
            sig = extract_leg_signature(pending)
            key = _duplicate_key(sig, pending.mode_id, discovery=self._discovery_mode)
            if key not in incumbents:
                incumbents[key] = pending.intent_id
            econ = extract_economic_exposure(pending)
            if econ is not None and econ not in economic_incumbents:
                economic_incumbents[econ] = pending.intent_id
                active_economic_keys.append((econ, pending.intent_id))

        def _candidate_rank(c: TradeIntent) -> tuple[Decimal, Decimal, str]:
            return (-c.strategy_confidence, c.estimated_max_loss.amount, c.intent_id)

        sorted_candidates = sorted(candidates, key=_candidate_rank)

        for candidate in sorted_candidates:
            sig = extract_leg_signature(candidate)
            risk_amt = candidate.requested_risk.amount
            family = _parse_family_id(candidate.family_id)
            mode_id = candidate.mode_id

            def _log_suppression(
                *,
                reason_code: ReasonCode,
                incumbent_id: str,
                detail: str,
                action: str,
            ) -> None:
                suppression = ArbitrationSuppression(
                    candidate_intent_id=candidate.intent_id,
                    candidate_mode_id=mode_id,
                    candidate_family_id=family,
                    incumbent_id=incumbent_id,
                    reason_code=reason_code,
                    detail=detail,
                )
                suppressed.append(suppression)
                suppressed_objects.append(id(candidate))
                counterfactuals.append(
                    CounterfactualLogEntry(
                        candidate_intent_id=candidate.intent_id,
                        mode_id=mode_id,
                        family_id=family,
                        action=action,
                        incumbent_id=incumbent_id,
                        timestamp=now,
                        requested_risk_amount=risk_amt,
                    )
                )

            def _log_soft_warning(
                *,
                reason_code: ReasonCode,
                incumbent_id: str | None,
                detail: str,
            ) -> None:
                soft_warnings.append(
                    ArbitrationSoftWarning(
                        intent_id=candidate.intent_id,
                        reason_code=reason_code,
                        incumbent_id=incumbent_id,
                        detail=detail,
                    )
                )

            dup_key = _duplicate_key(sig, mode_id, discovery=self._discovery_mode)
            if dup_key in incumbents:
                incumbent_id = incumbents[dup_key]
                _log_suppression(
                    reason_code=ReasonCode.EXACT_DUPLICATE_SUPPRESSED,
                    incumbent_id=incumbent_id,
                    detail=f"Exact duplicate suppressed; incumbent: {incumbent_id}",
                    action="SUPPRESSED_EXACT_DUPLICATE",
                )
                continue

            if self._discovery_mode and mode_id is not None:
                daily_cap = self._discovery.modes[mode_id.value].max_new_entries_per_day
                entries_today = mode_entry_counts.get(mode_id, 0)
                if entries_today >= daily_cap:
                    _log_suppression(
                        reason_code=ReasonCode.DAILY_ENTRY_CAP,
                        incumbent_id=f"{mode_id.value}:{entries_today}",
                        detail=(
                            f"Daily entry cap {daily_cap} reached for {mode_id.value}; "
                            f"entries today: {entries_today}"
                        ),
                        action="SUPPRESSED_DAILY_ENTRY_CAP",
                    )
                    continue
                open_cap = self._discovery.modes[mode_id.value].max_open_positions
                open_count = mode_open_counts.get(mode_id, 0)
                if open_count >= open_cap:
                    incumbent_id = (
                        _first_mode_incumbent(existing_positions, approved, mode_id)
                        or f"{mode_id.value}_CAP"
                    )
                    _log_suppression(
                        reason_code=ReasonCode.OPEN_POSITION_CAP,
                        incumbent_id=incumbent_id,
                        detail=(
                            f"Open position cap {open_cap} reached for {mode_id.value}; "
                            f"incumbent: {incumbent_id}"
                        ),
                        action="SUPPRESSED_OPEN_POSITION_CAP",
                    )
                    continue
            elif (
                candidate.mode_id is ModeId.M4_STRATEGIC_POSITIONAL
                and count_m4_positions(
                    existing_positions,
                    pending_m4_intents=tuple(
                        intent
                        for intent in approved
                        if intent.mode_id is ModeId.M4_STRATEGIC_POSITIONAL
                    ),
                )
                >= self._max_m4_open_positions
            ):
                incumbent_id = (
                    _first_m4_incumbent(existing_positions, approved) or "M4_CAP"
                )
                _log_suppression(
                    reason_code=ReasonCode.M4_POSITION_CAP_REACHED,
                    incumbent_id=incumbent_id,
                    detail=(
                        f"M4 position cap {self._max_m4_open_positions} reached; "
                        f"incumbent: {incumbent_id}"
                    ),
                    action="SUPPRESSED_M4_CAP",
                )
                continue

            candidate_econ = extract_economic_exposure(candidate)
            if candidate_econ is not None:
                overlap_incumbent = _find_economic_overlap(
                    candidate_econ, economic_incumbents
                )
                if overlap_incumbent is not None:
                    if self._discovery_mode:
                        _log_soft_warning(
                            reason_code=ReasonCode.ECONOMIC_OVERLAP_SUPPRESSED,
                            incumbent_id=overlap_incumbent,
                            detail=(
                                "Economic overlap would block under STRICT; "
                                f"incumbent: {overlap_incumbent}"
                            ),
                        )
                    else:
                        _log_suppression(
                            reason_code=ReasonCode.ECONOMIC_OVERLAP_SUPPRESSED,
                            incumbent_id=overlap_incumbent,
                            detail=(
                                "Economic overlap suppressed; same thesis band as "
                                f"incumbent: {overlap_incumbent}"
                            ),
                            action="SUPPRESSED_ECONOMIC_OVERLAP",
                        )
                        continue

                conflict_incumbent = _find_direction_conflict(
                    candidate_econ,
                    active_economic_keys,
                )
                if conflict_incumbent is not None:
                    if self._discovery_mode:
                        _log_soft_warning(
                            reason_code=ReasonCode.OPPOSING_EXPOSURE_REJECTED,
                            incumbent_id=conflict_incumbent,
                            detail=(
                                "Opposing exposure would block under STRICT; "
                                f"incumbent: {conflict_incumbent}"
                            ),
                        )
                    else:
                        _log_suppression(
                            reason_code=ReasonCode.OPPOSING_EXPOSURE_REJECTED,
                            incumbent_id=conflict_incumbent,
                            detail=(
                                "Opposing exposure rejected; conflicts with "
                                f"incumbent: {conflict_incumbent}"
                            ),
                            action="SUPPRESSED_OPPOSING_EXPOSURE",
                        )
                        continue

            approved.append(candidate)
            approved_objects.append(id(candidate))
            incumbents[dup_key] = candidate.intent_id
            if mode_id is not None:
                mode_entry_counts[mode_id] = mode_entry_counts.get(mode_id, 0) + 1
                mode_open_counts[mode_id] = mode_open_counts.get(mode_id, 0) + 1
            if candidate_econ is not None:
                economic_incumbents[candidate_econ] = candidate.intent_id
                active_economic_keys.append((candidate_econ, candidate.intent_id))
            counterfactuals.append(
                CounterfactualLogEntry(
                    candidate_intent_id=candidate.intent_id,
                    mode_id=mode_id,
                    family_id=family,
                    action="APPROVED",
                    incumbent_id=None,
                    timestamp=now,
                    requested_risk_amount=risk_amt,
                )
            )

        return ArbitrationResult(
            approved_intents=tuple(approved),
            suppressed_intents=tuple(suppressed),
            counterfactual_log=tuple(counterfactuals),
            approved_object_ids=tuple(approved_objects),
            suppressed_object_ids=tuple(suppressed_objects),
            soft_warnings=tuple(soft_warnings),
        )


def _duplicate_key(
    sig: tuple[str, str, _StructureSignature],
    mode_id: ModeId | None,
    *,
    discovery: bool,
) -> _IncumbentKey:
    if discovery:
        return (mode_id, sig)
    return (sig,)


def _initial_mode_open_counts(
    existing_positions: Sequence[PositionState | PositionLifecycleRecord],
    *,
    pending_intents: Sequence[TradeIntent],
) -> dict[ModeId, int]:
    counts: dict[ModeId, int] = {}
    for item in existing_positions:
        pos = item.position if isinstance(item, PositionLifecycleRecord) else item
        if pos.state not in {TradeState.OPEN, TradeState.PENDING_ENTRY}:
            continue
        mode_id = _position_mode_id(item)
        if mode_id is None:
            continue
        counts[mode_id] = counts.get(mode_id, 0) + 1
    for pending in pending_intents:
        if pending.mode_id is not None:
            counts[pending.mode_id] = counts.get(pending.mode_id, 0) + 1
    return counts


def _find_economic_overlap(
    candidate: EconomicExposureKey,
    incumbents: dict[EconomicExposureKey, str],
) -> str | None:
    for incumbent_key, incumbent_id in incumbents.items():
        if economic_keys_overlap(candidate, incumbent_key):
            return incumbent_id
    return None


def _find_direction_conflict(
    candidate: EconomicExposureKey,
    active: Sequence[tuple[EconomicExposureKey, str]],
) -> str | None:
    for incumbent_key, incumbent_id in active:
        if directions_conflict(candidate, incumbent_key):
            return incumbent_id
    return None


def _parse_family_id(value: str | None) -> FamilyId | None:
    if value is None:
        return None
    try:
        return FamilyId(value)
    except ValueError:
        return None


def _position_mode_id(item: PositionState | PositionLifecycleRecord) -> ModeId | None:
    if isinstance(item, PositionLifecycleRecord):
        return item.mode_id or item.position.mode_id
    return item.mode_id


def _first_mode_incumbent(
    existing_positions: Sequence[PositionState | PositionLifecycleRecord],
    approved: Sequence[TradeIntent],
    mode_id: ModeId,
) -> str | None:
    for item in existing_positions:
        pos = item.position if isinstance(item, PositionLifecycleRecord) else item
        resolved_mode = _position_mode_id(item)
        if resolved_mode is mode_id and pos.state in {
            TradeState.OPEN,
            TradeState.PENDING_ENTRY,
        }:
            return pos.trade_id
    for intent in approved:
        if intent.mode_id is mode_id:
            return intent.intent_id
    return None


def _first_m4_incumbent(
    existing_positions: Sequence[PositionState | PositionLifecycleRecord],
    approved: Sequence[TradeIntent],
) -> str | None:
    return _first_mode_incumbent(
        existing_positions, approved, ModeId.M4_STRATEGIC_POSITIONAL
    )
