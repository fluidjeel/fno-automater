"""Aggregated reconciliation outcome."""

from __future__ import annotations

from pydantic import model_validator

from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictBool,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.reconciliation import ReconciliationEvent
from trading.domain.enums import ReconciliationTrigger, Severity, SystemState

__all__ = ["ReconciliationResult"]


class ReconciliationResult(VersionedModel):
    """One reconciliation run and its effect on entry readiness."""

    result_id: NonEmptyStr
    trigger: ReconciliationTrigger
    events: tuple[ReconciliationEvent, ...]
    entries_blocked: StrictBool
    prior_system_state: SystemState
    resulting_system_state: SystemState
    broker_snapshot_ref: NonEmptyStr | None = None
    local_snapshot_ref: NonEmptyStr | None = None
    started_at: UtcDatetime
    completed_at: UtcDatetime

    @model_validator(mode="after")
    def _timing_and_blocking_agree(self) -> ReconciliationResult:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at precedes started_at")
        critical_unresolved = any(
            event.entries_blocked and not event.is_resolved for event in self.events
        )
        if critical_unresolved and not self.entries_blocked:
            raise ValueError(
                "entries_blocked must be true while unresolved blocking events exist"
            )
        if self.resulting_system_state is SystemState.READY and self.entries_blocked:
            raise ValueError("READY cannot coexist with entries_blocked")
        return self

    @property
    def has_critical_unresolved(self) -> bool:
        return any(
            event.severity is Severity.CRITICAL and not event.is_resolved
            for event in self.events
        )
