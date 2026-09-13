"""AIProposal: advisory output, stored for audit, never a broker instruction.

Invariants 2, 7 and 22. The type is shaped so the boundary is structural:

  - There is no field a proposal can set to promote itself, approve an order or
    name a quantity. Promotion happens in a separate signed configuration record
    that a proposal cannot write.
  - valid_until is mandatory, so a stale proposal cannot be consumed silently.
  - ABSTAIN is a normal value. Missing or unusable AI output must fall back to a
    deterministic baseline, not raise.
  - Prose lives in `narrative`, structurally apart from machine-consumable
    fields, so no consumer is tempted to parse it.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import ProposalType, Recommendation

__all__ = [
    "AIProposal",
    "EvidenceRef",
    "ModelVersions",
    "ParameterProposal",
]


class EvidenceRef(StrictModel):
    """One allowlisted source consulted for a proposal."""

    source_id: NonEmptyStr
    published_at: UtcDatetime
    retrieved_at: UtcDatetime
    allowlisted: StrictBool
    content_hash: NonEmptyStr

    @model_validator(mode="after")
    def _evidence_is_trustworthy_and_timely(self) -> EvidenceRef:
        if not self.allowlisted:
            raise ValueError(
                f"source {self.source_id} is not allowlisted; retrieved text is "
                "untrusted data and an unlisted source cannot ground a proposal"
            )
        if self.retrieved_at < self.published_at:
            raise ValueError("retrieved_at precedes published_at")
        return self


class ModelVersions(StrictModel):
    """Everything needed to reproduce or audit a proposal."""

    model: NonEmptyStr
    prompt_version: NonEmptyStr
    retrieval_version: NonEmptyStr
    policy_version: NonEmptyStr


class ParameterProposal(StrictModel):
    """A candidate value for one allowlisted, range-bounded parameter."""

    field_path: NonEmptyStr
    proposed_value: ExactDecimal
    allowed_minimum: ExactDecimal
    allowed_maximum: ExactDecimal

    @model_validator(mode="after")
    def _value_is_inside_the_allowed_range(self) -> ParameterProposal:
        if self.allowed_minimum > self.allowed_maximum:
            raise ValueError(
                f"{self.field_path}: allowed_minimum exceeds allowed_maximum"
            )
        if not self.allowed_minimum <= self.proposed_value <= self.allowed_maximum:
            raise ValueError(
                f"{self.field_path}: proposed {self.proposed_value} is outside the "
                f"allowed range [{self.allowed_minimum}, {self.allowed_maximum}]"
            )
        return self


class AIProposal(VersionedModel):
    """A bounded, expiring, evidence-linked recommendation. Never an order."""

    proposal_id: NonEmptyStr
    proposal_type: ProposalType
    scope: NonEmptyStr
    as_of_time: UtcDatetime
    valid_until: UtcDatetime
    versions: ModelVersions
    recommendation: Recommendation
    confidence: ExactDecimal = Field(ge=Decimal(0), le=Decimal(1))
    calibration_reference: NonEmptyStr | None = None
    evidence: tuple[EvidenceRef, ...] = ()
    parameter_proposals: tuple[ParameterProposal, ...] = ()
    assumptions: tuple[NonEmptyStr, ...] = ()
    contradictions: tuple[NonEmptyStr, ...] = ()
    missing_data: tuple[NonEmptyStr, ...] = ()
    narrative: str = ""

    @model_validator(mode="after")
    def _bounded_and_grounded(self) -> AIProposal:
        if self.valid_until <= self.as_of_time:
            raise ValueError(
                "valid_until must be after as_of_time; a proposal without a "
                "positive lifetime cannot be consumed safely"
            )
        if self.recommendation is Recommendation.ABSTAIN:
            if self.parameter_proposals:
                raise ValueError("an abstaining proposal must not propose parameters")
            return self
        if not self.evidence:
            raise ValueError(
                f"recommendation {self.recommendation} requires at least one "
                "allowlisted evidence reference; ABSTAIN when evidence is missing"
            )
        latest = max(ref.published_at for ref in self.evidence)
        if latest > self.as_of_time:
            raise ValueError(
                f"evidence published at {latest} is later than as_of_time "
                f"{self.as_of_time}; time-bounded retrieval was violated, which is "
                "lookahead in a research context and manipulation risk in a live one"
            )
        return self

    def is_usable_at(self, now: datetime) -> bool:
        """Whether a consumer may read this proposal at an injected instant."""
        return (
            self.as_of_time <= now < self.valid_until
            and self.recommendation is not Recommendation.ABSTAIN
        )
