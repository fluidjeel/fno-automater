"""PORTFOLIO desk contracts (ADESK-B6). Veto-only shared-fate assessments."""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import AgentAction, Severity, SharedFateCode

__all__ = [
    "ALLOWED_PORTFOLIO_ACTIONS",
    "PortfolioVeto",
    "SharedFateAssessment",
]

ALLOWED_PORTFOLIO_ACTIONS: frozenset[AgentAction] = frozenset(
    {
        AgentAction.VETO_ENTRY,
        AgentAction.REDUCE_SIZE,
        AgentAction.HOLD,
        AgentAction.ABSTAIN,
    }
)


class SharedFateAssessment(VersionedModel):
    existing_trade_id: NonEmptyStr
    code: SharedFateCode
    severity: Severity
    evidence_id: NonEmptyStr


class PortfolioVeto(VersionedModel):
    as_of: UtcDatetime
    candidate_trade_id: NonEmptyStr
    action: AgentAction
    shared_fate: tuple[SharedFateAssessment, ...] = ()
    size_multiplier: ExactDecimal = Field(le=Decimal("1"), ge=Decimal("0"))
    evidence_ids: tuple[NonEmptyStr, ...] = ()
    narrative: NonEmptyStr

    @model_validator(mode="after")
    def _action_allowed(self) -> PortfolioVeto:
        if self.action not in ALLOWED_PORTFOLIO_ACTIONS:
            raise ValueError(f"PORTFOLIO action not allowed: {self.action}")
        return self
