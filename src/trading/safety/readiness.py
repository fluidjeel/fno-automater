"""ENTRY_READY and LIVE_SAFE evaluation.

Invariant 6: stale or invalid critical state blocks new exposure.
Invariant 9: RECOVERY blocks entries until reconciliation completes.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading.domain.contracts.position import PositionState
from trading.domain.contracts.snapshot import FeatureSnapshot
from trading.domain.enums import (
    DataQuality,
    ReadinessLevel,
    ReasonCode,
    SystemState,
    TradeState,
)
from trading.safety.controls import SafetyControls

__all__ = ["ReadinessEvaluator", "ReadinessReport", "ReadinessRequest"]


@dataclass(frozen=True, slots=True)
class ReadinessRequest:
    """Inputs for one readiness evaluation."""

    system_state: SystemState
    entries_blocked: bool
    feature_snapshot: FeatureSnapshot | None
    safety_controls: SafetyControls
    open_positions: tuple[PositionState, ...] = ()
    strategy_id: str | None = None
    clock_drift_ms: int = 0
    max_clock_drift_ms: int | None = None


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Outcome of one readiness evaluation."""

    levels: dict[ReadinessLevel, bool]
    reason_codes: tuple[ReasonCode, ...]
    entries_permitted: bool


class ReadinessEvaluator:
    """Evaluate operational readiness levels independently."""

    def evaluate(self, request: ReadinessRequest) -> ReadinessReport:
        """Return readiness levels and whether new exposure is permitted."""
        reasons = self._entry_block_reasons(request)
        live_safe = self._live_safe(request)
        entry_ready = live_safe and not reasons
        research_ready = _research_ready(request)
        return ReadinessReport(
            levels={
                ReadinessLevel.LIVE_SAFE: live_safe,
                ReadinessLevel.ENTRY_READY: entry_ready,
                ReadinessLevel.RESEARCH_READY: research_ready,
            },
            reason_codes=reasons,
            entries_permitted=entry_ready,
        )

    def _live_safe(self, request: ReadinessRequest) -> bool:
        if request.entries_blocked:
            return False
        for position in request.open_positions:
            if position.state is TradeState.REPAIR_REQUIRED:
                return False
            if (
                position.state.requires_protective_coverage
                and not position.protective_order_ids
            ):
                return False
        return True

    def _entry_block_reasons(self, request: ReadinessRequest) -> tuple[ReasonCode, ...]:
        reasons: list[ReasonCode] = []

        if not request.system_state.permits_new_exposure:
            reasons.append(ReasonCode.SYSTEM_NOT_READY)
        if request.entries_blocked:
            reasons.append(ReasonCode.RECONCILIATION_UNRESOLVED)

        control_reasons = request.safety_controls.entry_block_reasons(
            request.strategy_id
        )
        reasons.extend(control_reasons)

        feature_reasons = _feature_block_reasons(
            request.feature_snapshot,
            max_clock_drift_ms=request.max_clock_drift_ms,
            clock_drift_ms=request.clock_drift_ms,
        )
        reasons.extend(feature_reasons)

        return tuple(dict.fromkeys(reasons))


def _feature_block_reasons(
    feature_snapshot: FeatureSnapshot | None,
    *,
    max_clock_drift_ms: int | None,
    clock_drift_ms: int,
) -> tuple[ReasonCode, ...]:
    if feature_snapshot is None:
        return (ReasonCode.DATA_INVALID,)
    if not feature_snapshot.permits_new_exposure:
        return (_quality_reason(feature_snapshot.quality.state),)
    if max_clock_drift_ms is not None and clock_drift_ms > max_clock_drift_ms:
        return (ReasonCode.CLOCK_DRIFT,)
    return ()


def _research_ready(request: ReadinessRequest) -> bool:
    """Research readiness is independent of entry gating."""
    feature = request.feature_snapshot
    if feature is None:
        return False
    return feature.quality.state is not DataQuality.INVALID


def _quality_reason(state: DataQuality) -> ReasonCode:
    if state is DataQuality.STALE:
        return ReasonCode.DATA_STALE
    if state is DataQuality.INVALID:
        return ReasonCode.DATA_INVALID
    return ReasonCode.DATA_DEGRADED
