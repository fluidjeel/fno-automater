"""MACRO desk contracts: MacroCalendar + MacroAssessment (ADESK-B7)."""

from __future__ import annotations

from datetime import date

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import AgentAction, EventClass, MacroEventSeverity, Severity

__all__ = [
    "MacroAssessment",
    "MacroCalendar",
    "MacroCalendarEvent",
    "scheduled_event_blocks_entry",
]


class MacroCalendarEvent(StrictModel):
    event_id: NonEmptyStr
    event_date: date
    event_class: EventClass
    scheduled: StrictBool
    horizon_sessions: StrictInt = Field(ge=0)
    direction_uncertainty: MacroEventSeverity
    label: NonEmptyStr


class MacroCalendar(VersionedModel):
    as_of: UtcDatetime
    events: tuple[MacroCalendarEvent, ...] = ()
    calendar_version: NonEmptyStr

    def high_uncertainty_on(self, day: date) -> tuple[MacroCalendarEvent, ...]:
        return tuple(
            e
            for e in self.events
            if e.scheduled
            and e.event_date == day
            and e.direction_uncertainty is MacroEventSeverity.HIGH
        )


def scheduled_event_blocks_entry(
    calendar: MacroCalendar,
    *,
    session_date: date,
    horizon_days: int,
    defined_risk: bool,
    under_single_event_cap: bool,
) -> bool:
    """Deterministic gate: block positional entry spanning high-uncertainty events."""
    if horizon_days <= 0:
        return False
    # Span: session_date .. session_date+horizon_days (inclusive window of days)
    spanned = {
        date.fromordinal(session_date.toordinal() + offset)
        for offset in range(horizon_days + 1)
    }
    hits = [
        e
        for e in calendar.events
        if e.scheduled
        and e.event_date in spanned
        and e.direction_uncertainty is MacroEventSeverity.HIGH
    ]
    if not hits:
        return False
    return not (defined_risk and under_single_event_cap)


class MacroAssessment(VersionedModel):
    as_of: UtcDatetime
    cluster_ids: tuple[NonEmptyStr, ...]
    event_class: EventClass
    materiality: Severity
    affected_conditions: tuple[NonEmptyStr, ...] = ()
    affected_trade_ids: tuple[NonEmptyStr, ...] = ()
    action: AgentAction
    evidence_ids: tuple[NonEmptyStr, ...] = ()
    narrative: NonEmptyStr

    @model_validator(mode="after")
    def _action_ok(self) -> MacroAssessment:
        allowed = {
            AgentAction.VETO_ENTRY,
            AgentAction.REDUCE_SIZE,
            AgentAction.HOLD,
            AgentAction.ABSTAIN,
        }
        if self.action not in allowed:
            raise ValueError(f"MACRO action not allowed: {self.action}")
        return self
