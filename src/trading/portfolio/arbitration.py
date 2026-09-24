"""Portfolio Arbitration: exact duplicates, economic overlap, conflict, M4 cap (P4/P12)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from trading.domain.contracts.base import StrictModel
from trading.domain.contracts.intent import IntentLeg, TradeIntent
from trading.domain.contracts.lifecycle import PositionLifecycleRecord
from trading.domain.contracts.position import PositionState
from trading.domain.enums import FamilyId, ModeId, ReasonCode, TradeState
from trading.portfolio.economic_overlap import (
    MAX_M4_OPEN_POSITIONS,
    EconomicExposureKey,
    count_m4_positions,
    directions_conflict,
    economic_keys_overlap,
    extract_economic_exposure,
)

__all__ = [
    "ArbitrationResult",
    "ArbitrationSuppression",
    "CounterfactualLogEntry",
    "PortfolioArbiter",
    "extract_leg_signature",
]

# Canonical signature type for normalized option structures:
# tuple of ((symbol, side, ratio), ...) sorted alphabetically
_StructureSignature = tuple[tuple[str, str, int], ...]


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


@dataclass(frozen=True, slots=True)
class ArbitrationSuppression:
    """Record of an intent suppressed by arbitration with reference to incumbent."""

    candidate_intent_id: str
    candidate_mode_id: ModeId | None
    candidate_family_id: FamilyId | None
    incumbent_id: str
    reason_code: ReasonCode
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


class PortfolioArbiter:
    """Arbitrates candidates from multiple mode producers."""

    def arbitrate(
        self,
        candidates: Sequence[TradeIntent],
        *,
        existing_positions: Sequence[PositionState | PositionLifecycleRecord] = (),
        pending_intents: Sequence[TradeIntent] = (),
        now: datetime,
    ) -> ArbitrationResult:
        """Arbitrate candidate intents with duplicate, overlap, conflict, and M4 cap rules."""
        approved: list[TradeIntent] = []
        approved_objects: list[int] = []
        suppressed: list[ArbitrationSuppression] = []
        suppressed_objects: list[int] = []
        counterfactuals: list[CounterfactualLogEntry] = []

        incumbents: dict[tuple[str, str, _StructureSignature], str] = {}
        economic_incumbents: dict[EconomicExposureKey, str] = {}
        active_economic_keys: list[tuple[EconomicExposureKey, str]] = []

        for item in existing_positions:
            pos = item.position if isinstance(item, PositionLifecycleRecord) else item
            if pos.state not in {TradeState.OPEN, TradeState.PENDING_ENTRY}:
                continue
            if not pos.legs:
                continue
            underlying = pos.legs[0].contract.underlying
            expiry_str = (
                pos.legs[0].contract.expiry.isoformat()
                if pos.legs[0].contract.expiry
                else "NONE"
            )
            sig = (underlying, expiry_str, _normalize_position_legs(pos))
            incumbents[sig] = pos.trade_id
            intent = item.intent if isinstance(item, PositionLifecycleRecord) else None
            if intent is not None:
                econ = extract_economic_exposure(intent)
                if econ is not None:
                    economic_incumbents[econ] = pos.trade_id
                    active_economic_keys.append((econ, pos.trade_id))

        for pending in pending_intents:
            sig = extract_leg_signature(pending)
            if sig not in incumbents:
                incumbents[sig] = pending.intent_id
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

            def _log_suppression(
                *,
                reason_code: ReasonCode,
                incumbent_id: str,
                detail: str,
                action: str,
            ) -> None:
                suppression = ArbitrationSuppression(
                    candidate_intent_id=candidate.intent_id,
                    candidate_mode_id=candidate.mode_id,
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
                        mode_id=candidate.mode_id,
                        family_id=family,
                        action=action,
                        incumbent_id=incumbent_id,
                        timestamp=now,
                        requested_risk_amount=risk_amt,
                    )
                )

            if sig in incumbents:
                incumbent_id = incumbents[sig]
                _log_suppression(
                    reason_code=ReasonCode.EXACT_DUPLICATE_SUPPRESSED,
                    incumbent_id=incumbent_id,
                    detail=f"Exact duplicate suppressed; incumbent: {incumbent_id}",
                    action="SUPPRESSED_EXACT_DUPLICATE",
                )
                continue

            m4_count = count_m4_positions(
                existing_positions,
                pending_m4_intents=tuple(
                    intent
                    for intent in approved
                    if intent.mode_id is ModeId.M4_STRATEGIC_POSITIONAL
                ),
            )
            if (
                candidate.mode_id is ModeId.M4_STRATEGIC_POSITIONAL
                and m4_count >= MAX_M4_OPEN_POSITIONS
            ):
                incumbent_id = (
                    _first_m4_incumbent(existing_positions, approved) or "M4_CAP"
                )
                _log_suppression(
                    reason_code=ReasonCode.M4_POSITION_CAP_REACHED,
                    incumbent_id=incumbent_id,
                    detail=(
                        f"M4 position cap {MAX_M4_OPEN_POSITIONS} reached; "
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
            incumbents[sig] = candidate.intent_id
            if candidate_econ is not None:
                economic_incumbents[candidate_econ] = candidate.intent_id
                active_economic_keys.append((candidate_econ, candidate.intent_id))
            counterfactuals.append(
                CounterfactualLogEntry(
                    candidate_intent_id=candidate.intent_id,
                    mode_id=candidate.mode_id,
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
        )


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


def _first_m4_incumbent(
    existing_positions: Sequence[PositionState | PositionLifecycleRecord],
    approved: Sequence[TradeIntent],
) -> str | None:
    for item in existing_positions:
        pos = item.position if isinstance(item, PositionLifecycleRecord) else item
        if pos.mode_id is ModeId.M4_STRATEGIC_POSITIONAL and pos.state in {
            TradeState.OPEN,
            TradeState.PENDING_ENTRY,
        }:
            return pos.trade_id
    for intent in approved:
        if intent.mode_id is ModeId.M4_STRATEGIC_POSITIONAL:
            return intent.intent_id
    return None
