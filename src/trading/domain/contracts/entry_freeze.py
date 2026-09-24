"""Persisted entry-freeze latch. In-memory SafetyControls are not authority."""

from __future__ import annotations

from pydantic import model_validator

from trading.domain.contracts.base import StrictBool, UtcDatetime, VersionedModel
from trading.domain.enums import ModeId, ReasonCode

__all__ = ["EntryFreezeRecord"]


class EntryFreezeRecord(VersionedModel):
    """Singleton snapshot of whether new PAPER/LIVE entries are blocked."""

    entries_blocked: StrictBool
    reason_code: ReasonCode | None = None
    detail: str | None = None
    updated_at: UtcDatetime
    mode_id: ModeId | None = None

    @model_validator(mode="after")
    def _blocked_state_has_a_reason(self) -> EntryFreezeRecord:
        if self.entries_blocked and self.reason_code is None:
            raise ValueError("a freeze must record a machine-readable reason_code")
        if self.entries_blocked and (self.detail is None or self.detail == ""):
            raise ValueError("a freeze must record a human-readable detail")
        if not self.entries_blocked and self.reason_code is not None:
            raise ValueError("a cleared freeze must not keep a blocking reason_code")
        return self
