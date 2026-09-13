"""ReconciliationEvent: evidence for every comparison against broker truth.

Invariant 5: broker-reported state is external truth.
Invariant 25: every recovery transition is durably auditable, so a comparison
that finds nothing still produces a record.
"""

from __future__ import annotations

from pydantic import model_validator

from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictBool,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import (
    DifferenceClass,
    ReasonCode,
    ReconciliationTrigger,
    Severity,
)

__all__ = ["ReconciliationEvent"]

_UNRESOLVED_UNTIL_REPAIRED = frozenset(
    {
        DifferenceClass.MISSING_LOCAL_EVENT,
        DifferenceClass.UNEXPECTED_BROKER_STATE,
        DifferenceClass.LOCAL_ORDER_ABSENT_AT_BROKER,
    }
)


class ReconciliationEvent(VersionedModel):
    """One comparison of local expected state against broker-observed state."""

    event_id: NonEmptyStr
    scope: NonEmptyStr
    trigger: ReconciliationTrigger
    expected_local_ref: NonEmptyStr | None = None
    observed_broker_ref: NonEmptyStr | None = None
    difference_class: DifferenceClass
    severity: Severity
    reason_code: ReasonCode
    repair_action: NonEmptyStr | None = None
    repair_succeeded: StrictBool | None = None
    entries_blocked: StrictBool
    detected_at: UtcDatetime
    resolved_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _classification_and_repair_agree(self) -> ReconciliationEvent:
        matched = self.difference_class is DifferenceClass.NONE

        if matched:
            if self.severity is not Severity.INFO:
                raise ValueError("a matched comparison cannot be above INFO severity")
            if self.repair_action is not None:
                raise ValueError("a matched comparison needs no repair action")
            if self.reason_code is not ReasonCode.OK:
                raise ValueError("a matched comparison records ReasonCode.OK")
        elif self.reason_code is ReasonCode.OK:
            raise ValueError(
                f"difference {self.difference_class} cannot be explained by OK"
            )

        if self.severity is Severity.CRITICAL and self.repair_action is None:
            raise ValueError(
                "a critical discrepancy requires a deterministic repair action; "
                "leaving it unhandled would let entries resume over unknown exposure"
            )
        if self.repair_succeeded is not None and self.repair_action is None:
            raise ValueError("a repair outcome without a repair action is incoherent")

        if self.resolved_at is not None:
            if self.resolved_at < self.detected_at:
                raise ValueError("resolved_at precedes detected_at")
            if self.repair_succeeded is False:
                raise ValueError(
                    "a failed repair is not resolved; leave resolved_at unset so the "
                    "break stays visible to the runbook"
                )
        elif self.repair_succeeded:
            raise ValueError("a successful repair must record resolved_at")

        if (
            self.difference_class in _UNRESOLVED_UNTIL_REPAIRED
            and not self.repair_succeeded
            and not self.entries_blocked
        ):
            raise ValueError(
                f"difference {self.difference_class} is unrepaired, so entries must "
                "stay blocked for the affected scope until it resolves"
            )
        return self

    @property
    def is_resolved(self) -> bool:
        return self.resolved_at is not None and self.repair_succeeded is not False
