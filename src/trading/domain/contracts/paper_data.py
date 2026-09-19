"""Explicit two-tier PAPER market-data contract.

P0 fields are required for every paper entry and fail closed when missing,
zero-invalid, or stale. P1 fields improve identification when observed; they
are never invented.
"""

from __future__ import annotations

from enum import StrEnum, unique

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    VersionedModel,
)
from trading.domain.enums import ReasonCode

__all__ = [
    "P0_FIELDS",
    "P1_FIELDS",
    "PaperDataAssessment",
    "PaperDataField",
    "PaperDataFieldResult",
    "PaperDataFieldSpec",
    "PaperDataPresence",
    "PaperDataRequirements",
    "PaperDataTier",
]


@unique
class PaperDataTier(StrEnum):
    P0 = "P0"
    P1 = "P1"


@unique
class PaperDataField(StrEnum):
    LTP = "LTP"
    BID_ASK = "BID_ASK"
    QUOTE_FRESHNESS = "QUOTE_FRESHNESS"
    VOLUME = "VOLUME"
    OPEN_INTEREST = "OPEN_INTEREST"
    CONTRACT_METADATA = "CONTRACT_METADATA"
    MARGIN_ESTIMATE = "MARGIN_ESTIMATE"
    POSITION_BROKER_STATE = "POSITION_BROKER_STATE"
    EVENT_STATE = "EVENT_STATE"
    IV_SURFACE = "IV_SURFACE"
    IV_SKEW = "IV_SKEW"
    TERM_STRUCTURE = "TERM_STRUCTURE"
    REALIZED_VOLATILITY = "REALIZED_VOLATILITY"
    GREEKS = "GREEKS"
    DEPTH = "DEPTH"


@unique
class PaperDataPresence(StrEnum):
    PRESENT = "PRESENT"
    MISSING = "MISSING"
    STALE = "STALE"
    ZERO_INVALID = "ZERO_INVALID"
    SKIPPED = "SKIPPED"


P0_FIELDS: frozenset[PaperDataField] = frozenset(
    {
        PaperDataField.LTP,
        PaperDataField.BID_ASK,
        PaperDataField.QUOTE_FRESHNESS,
        PaperDataField.VOLUME,
        PaperDataField.OPEN_INTEREST,
        PaperDataField.CONTRACT_METADATA,
        PaperDataField.MARGIN_ESTIMATE,
        PaperDataField.POSITION_BROKER_STATE,
        PaperDataField.EVENT_STATE,
    }
)
P1_FIELDS: frozenset[PaperDataField] = frozenset(
    {
        PaperDataField.IV_SURFACE,
        PaperDataField.IV_SKEW,
        PaperDataField.TERM_STRUCTURE,
        PaperDataField.REALIZED_VOLATILITY,
        PaperDataField.GREEKS,
        PaperDataField.DEPTH,
    }
)

_FIELD_TIER = {field: PaperDataTier.P0 for field in P0_FIELDS} | {
    field: PaperDataTier.P1 for field in P1_FIELDS
}


class PaperDataFieldSpec(StrictModel):
    """One configured field: tier, freshness SLA, and fail-closed flags."""

    field: PaperDataField
    tier: PaperDataTier
    max_age_ms: StrictInt | None = Field(default=None, gt=0)
    zero_invalid: StrictBool = False
    min_points: StrictInt | None = Field(default=None, ge=1)
    block_families: tuple[NonEmptyStr, ...] = ()

    @model_validator(mode="after")
    def _tier_matches_field(self) -> PaperDataFieldSpec:
        expected = _FIELD_TIER[self.field]
        if self.tier is not expected:
            raise ValueError(
                f"{self.field} is a {expected} field; config declared {self.tier}"
            )
        return self


class PaperDataRequirements(VersionedModel):
    """Closed P0/P1 lists plus per-field freshness. Config, not code constants."""

    requirements_version: NonEmptyStr
    fields: tuple[PaperDataFieldSpec, ...]

    @model_validator(mode="after")
    def _lists_are_complete_and_unique(self) -> PaperDataRequirements:
        seen: list[PaperDataField] = []
        for spec in self.fields:
            if spec.field in seen:
                raise ValueError(f"duplicate paper-data field {spec.field}")
            seen.append(spec.field)
        present = set(seen)
        missing_p0 = P0_FIELDS - present
        if missing_p0:
            raise ValueError(
                "P0 paper-data contract is incomplete; missing "
                + ", ".join(sorted(field.value for field in missing_p0))
            )
        missing_p1 = P1_FIELDS - present
        if missing_p1:
            raise ValueError(
                "P1 paper-data contract is incomplete; missing "
                + ", ".join(sorted(field.value for field in missing_p1))
            )
        return self

    @property
    def p0(self) -> tuple[PaperDataFieldSpec, ...]:
        return tuple(spec for spec in self.fields if spec.tier is PaperDataTier.P0)

    @property
    def p1(self) -> tuple[PaperDataFieldSpec, ...]:
        return tuple(spec for spec in self.fields if spec.tier is PaperDataTier.P1)

    def spec_for(self, field: PaperDataField) -> PaperDataFieldSpec:
        for spec in self.fields:
            if spec.field is field:
                return spec
        raise KeyError(field)


class PaperDataFieldResult(StrictModel):
    """Audit row for one field in one assessment."""

    field: PaperDataField
    tier: PaperDataTier
    presence: PaperDataPresence
    reason_code: ReasonCode | None = None
    detail: NonEmptyStr | None = None


class PaperDataAssessment(StrictModel):
    """P0 gate outcome plus observed-vs-absent P1, never invented values."""

    requirements_version: NonEmptyStr
    p0_ok: StrictBool
    results: tuple[PaperDataFieldResult, ...]

    @property
    def p0_reason_codes(self) -> tuple[ReasonCode, ...]:
        codes = [
            row.reason_code
            for row in self.results
            if row.tier is PaperDataTier.P0 and row.reason_code is not None
        ]
        return tuple(dict.fromkeys(codes))

    @property
    def failed_p0_fields(self) -> tuple[PaperDataField, ...]:
        return tuple(
            row.field
            for row in self.results
            if row.tier is PaperDataTier.P0
            and row.presence
            in {
                PaperDataPresence.MISSING,
                PaperDataPresence.STALE,
                PaperDataPresence.ZERO_INVALID,
            }
        )

    @property
    def p1_present(self) -> tuple[PaperDataField, ...]:
        return tuple(
            row.field
            for row in self.results
            if row.tier is PaperDataTier.P1
            and row.presence is PaperDataPresence.PRESENT
        )

    @property
    def p1_absent(self) -> tuple[PaperDataField, ...]:
        return tuple(
            row.field
            for row in self.results
            if (
                row.tier is PaperDataTier.P1
                and row.presence is PaperDataPresence.MISSING
            )
        )
