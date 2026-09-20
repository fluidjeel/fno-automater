"""ImprovementRecord contract (ADESK-A7 / AGENT_DESK_SPEC PART 9.2)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictInt,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import (
    DeskRole,
    ImprovementArea,
    ImprovementStatus,
    TestabilityKind,
)

__all__ = ["ImprovementCluster", "ImprovementRecord"]


class ImprovementRecord(VersionedModel):
    """Structured improvement observation. Never auto-implemented."""

    record_id: NonEmptyStr
    opened_at: UtcDatetime
    author: DeskRole
    area: ImprovementArea
    claim: NonEmptyStr
    supporting_trade_ids: tuple[NonEmptyStr, ...]
    proposed_change: NonEmptyStr
    testable_as: TestabilityKind
    status: ImprovementStatus = ImprovementStatus.OPEN
    occurrences: StrictInt = Field(default=1, ge=1)
    estimated_cost_r: ExactDecimal = Field(default=Decimal("0"), ge=Decimal(0))
    claim_key: NonEmptyStr

    @field_validator("claim", "proposed_change")
    @classmethod
    def _strip_nonempty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must be non-empty after strip")
        return cleaned

    @model_validator(mode="after")
    def _require_supporting_trades(self) -> ImprovementRecord:
        if len(self.supporting_trade_ids) < 1:
            raise ValueError("supporting_trade_ids must contain at least one trade_id")
        return self


class ImprovementCluster(VersionedModel):
    """Ranked cluster sharing an area + claim_key."""

    cluster_id: NonEmptyStr
    area: ImprovementArea
    claim_key: NonEmptyStr
    occurrences: StrictInt = Field(ge=1)
    estimated_cost_r_total: ExactDecimal
    rank_score: ExactDecimal
    record_ids: tuple[NonEmptyStr, ...]
    status: ImprovementStatus
