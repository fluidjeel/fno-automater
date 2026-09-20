"""Review-level judgment metrics (ADESK-B4)."""

from __future__ import annotations

from decimal import Decimal

from pydantic import Field

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import ReviewAction

__all__ = ["ReviewJudgmentReport", "ReviewJudgmentRow"]


class ReviewJudgmentRow(StrictModel):
    """One review labelled ex-post: was HOLD better than EXIT at this slot?"""

    review_id: NonEmptyStr
    trade_id: NonEmptyStr
    action: ReviewAction
    should_hold: StrictBool | None = None
    held: StrictBool
    subsequent_r: ExactDecimal | None = None


class ReviewJudgmentReport(VersionedModel):
    """Cohort review precision and capture (PART 14 POSITION metrics seed)."""

    as_of: UtcDatetime
    labeled_count: StrictInt = Field(ge=0)
    hold_correct_count: StrictInt = Field(ge=0)
    exit_correct_count: StrictInt = Field(ge=0)
    precision: ExactDecimal | None = Field(default=None, ge=Decimal(0), le=Decimal(1))
    capture: ExactDecimal | None = Field(default=None, ge=Decimal(0), le=Decimal(1))
    rows: tuple[ReviewJudgmentRow, ...] = ()
