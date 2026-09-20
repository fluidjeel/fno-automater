"""PAPER position protection state. Software-only; not broker-resident."""

from __future__ import annotations

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import ProtectionStatus, QuoteMonitorSource, ReasonCode

__all__ = [
    "ProtectionHeartbeat",
    "ProtectionObservation",
    "ProtectionStateRecord",
    "SessionProtectionState",
]


class ProtectionObservation(StrictModel):
    """One quote sample used for exit evaluation on an open trade."""

    symbol: NonEmptyStr
    source: QuoteMonitorSource
    observed_at: UtcDatetime
    bid: NonEmptyStr | None = None
    ask: NonEmptyStr | None = None
    snapshot_id: NonEmptyStr | None = None


class ProtectionStateRecord(VersionedModel):
    """Durable per-trade protection monitor state."""

    trade_id: NonEmptyStr
    status: ProtectionStatus
    reason_code: ReasonCode | None = None
    degraded_since: UtcDatetime | None = None
    last_fresh_quote_at: UtcDatetime | None = None
    last_heartbeat_at: UtcDatetime | None = None
    monitor_symbols: tuple[NonEmptyStr, ...] = ()
    observations: tuple[ProtectionObservation, ...] = ()
    as_of: UtcDatetime

    @model_validator(mode="after")
    def _degraded_has_reason(self) -> ProtectionStateRecord:
        if self.status is ProtectionStatus.DEGRADED and self.reason_code is None:
            raise ValueError("degraded protection requires a reason_code")
        return self


class SessionProtectionState(VersionedModel):
    """Aggregate session protection for heartbeat and watchdog."""

    as_of: UtcDatetime
    open_positions: int = Field(ge=0)
    monitor_active: bool
    ws_connected: bool
    protection_degraded: bool
    last_quote_at: UtcDatetime | None = None
    monitor_symbols: tuple[NonEmptyStr, ...] = ()


class ProtectionHeartbeat(StrictModel):
    """JSON heartbeat written for the external watchdog process."""

    as_of: UtcDatetime
    open_positions: int = Field(ge=0)
    monitor_active: bool
    last_quote_at: UtcDatetime | None = None
    ws_connected: bool = False
    protection_degraded: bool = False
