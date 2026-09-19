"""Operator attention: a structured blocker, never a live command.

Invariant 2: this type cannot place an order, mutate config, or invoke a
safety control. It names what the operator must supply so Layer 4 can continue.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import model_validator

from trading.domain.contracts.base import NonEmptyStr, UtcDatetime, VersionedModel
from trading.domain.contracts.proposal import EvidenceRef
from trading.domain.enums import AttentionBlocker, ReasonCode

__all__ = ["AttentionRequest"]


class AttentionRequest(VersionedModel):
    """One operator checkpoint. Advisory only."""

    request_id: NonEmptyStr
    blocker: AttentionBlocker
    reason_code: ReasonCode
    detail: NonEmptyStr
    required_artifact: NonEmptyStr
    as_of_time: UtcDatetime
    valid_until: UtcDatetime
    evidence: tuple[EvidenceRef, ...] = ()

    @model_validator(mode="after")
    def _lifetime_is_positive(self) -> AttentionRequest:
        if self.valid_until <= self.as_of_time:
            raise ValueError("valid_until must be after as_of_time")
        return self

    def is_open_at(self, now: datetime) -> bool:
        """Whether the request is still waiting on the operator."""
        return self.as_of_time <= now < self.valid_until
