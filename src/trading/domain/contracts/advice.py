"""StructureAdvice: ranked paper-family recommendation. Never a live order."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated

from pydantic import Field, field_validator, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)

__all__ = [
    "ALLOWED_STRUCTURES",
    "AdviceStance",
    "RankedStructure",
    "StructureAdvice",
    "StructureChoice",
]


class StructureChoice(StrEnum):
    """Paper families the advise desk may pick, plus PASS."""

    POSITIONAL_LONG_OPTION = "positional_long_option"
    DEBIT_SPREAD = "debit_spread"
    DEFINED_RISK_MULTILEG = "defined_risk_multileg"
    CAS_MICROSTRUCTURE = "cas_microstructure"
    COMMODITY_FUTURES_TREND = "commodity_futures_trend"
    PASS = "PASS"


ALLOWED_STRUCTURES: frozenset[str] = frozenset(item.value for item in StructureChoice)


class AdviceStance(StrEnum):
    """Paper/shadow posture for the preferred structure. Never live ENABLE."""

    PAPER = "PAPER"
    SHADOW = "SHADOW"
    SUSPENDED = "SUSPENDED"
    PASS = "PASS"


class RankedStructure(StrictModel):
    """One alternative in the ranked list."""

    structure: StructureChoice
    score: ExactDecimal = Field(ge=0, le=1)
    why: NonEmptyStr


class StructureAdvice(VersionedModel):
    """Bounded structure-desk output for `trading agent advise`."""

    as_of: UtcDatetime
    preferred_structure: StructureChoice
    stance: AdviceStance
    confidence: ExactDecimal = Field(ge=0, le=1)
    alternatives_ranked: tuple[RankedStructure, ...] = ()
    do_not_trade_if: tuple[NonEmptyStr, ...] = ()
    invalidation: tuple[NonEmptyStr, ...] = ()
    evidence_ids: tuple[NonEmptyStr, ...] = ()
    failed_gate_ids: tuple[NonEmptyStr, ...] = ()
    market_summary: dict[str, object] = Field(default_factory=dict)

    @field_validator("preferred_structure", mode="before")
    @classmethod
    def _reject_unknown_structure(cls, value: object) -> object:
        if isinstance(value, StructureChoice):
            return value
        if isinstance(value, str) and value not in ALLOWED_STRUCTURES:
            raise ValueError(
                f"unknown preferred_structure {value!r}; "
                f"allowed={sorted(ALLOWED_STRUCTURES)}"
            )
        return value

    @model_validator(mode="after")
    def _pass_is_coherent(self) -> StructureAdvice:
        if self.preferred_structure is StructureChoice.PASS and self.stance is not AdviceStance.PASS:
            raise ValueError("PASS structure requires stance PASS")
        if self.stance is AdviceStance.PASS and self.preferred_structure is not StructureChoice.PASS:
            raise ValueError("stance PASS requires preferred_structure PASS")
        return self
