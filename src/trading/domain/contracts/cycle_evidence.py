"""Durable per-cycle observability evidence. Not a live trading authority."""

from __future__ import annotations

from pydantic import Field

from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictBool,
    StrictInt,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.identification import RouteDecision, SetupFeatures
from trading.domain.enums import (
    ExecutionMode,
    FunnelStage,
    ModeId,
    ReasonCode,
    SystemState,
)

__all__ = [
    "ActivityFunnelSummary",
    "FamilyOperatorRow",
    "FunnelDrop",
    "PaperCycleEvidence",
    "StrategyCycleSummary",
]


class FunnelDrop(VersionedModel):
    """One abstention or rejection recorded on the activity funnel."""

    stage: FunnelStage
    strategy_id: NonEmptyStr
    mode_id: ModeId | None = None
    family_id: NonEmptyStr | None = None
    reason_codes: tuple[ReasonCode, ...] = ()
    detail: NonEmptyStr = "unspecified"


class ActivityFunnelSummary(VersionedModel):
    """Per-cycle funnel lineage including flat reasons and G1 abstentions."""

    drops: tuple[FunnelDrop, ...] = ()
    approved_count: StrictInt = Field(ge=0, default=0)
    suppressed_count: StrictInt = Field(ge=0, default=0)


class FamilyOperatorRow(VersionedModel):
    """Operator-facing G1/G2 and stance row for one family."""

    family_id: NonEmptyStr
    mode_id: ModeId | None = None
    session_stance: ExecutionMode | None = None
    research_status: NonEmptyStr
    g1_blocked: StrictBool = False
    g2_proven: StrictBool = False
    next_review_slot: NonEmptyStr | None = None


class StrategyCycleSummary(VersionedModel):
    """One strategy's outcome summary within a supervised paper cycle."""

    strategy_id: NonEmptyStr
    snapshot_id: NonEmptyStr
    execution_mode: ExecutionMode
    executed: StrictBool
    rejection_reasons: tuple[ReasonCode, ...] = ()
    entry_blocked_reasons: tuple[ReasonCode, ...] = ()
    intent_count: StrictInt = Field(ge=0)
    setup_features: SetupFeatures | None = None


class PaperCycleEvidence(VersionedModel):
    """Frozen identification, routing and abstention evidence for one cycle."""

    cycle_id: NonEmptyStr
    as_of: UtcDatetime
    system_state: SystemState
    entries_blocked: StrictBool
    reconcile_id: NonEmptyStr
    route_decision: RouteDecision | None = None
    strategies: tuple[StrategyCycleSummary, ...]
    funnel: ActivityFunnelSummary | None = None
