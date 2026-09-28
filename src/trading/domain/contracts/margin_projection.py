"""DISCOVERY margin projection adjustments when broker margin is oversubscribed."""

from __future__ import annotations

from pydantic import model_validator

from trading.domain.contracts.base import StrictModel
from trading.domain.primitives import Money

__all__ = ["MarginProjectionAdjustment"]


class MarginProjectionAdjustment(StrictModel):
    """Records broker margin oversubscription clamped under DISCOVERY."""

    pre_trade_margin_available: Money
    margin_required: Money
    oversubscription: Money

    @model_validator(mode="after")
    def _oversubscription_is_positive(self) -> MarginProjectionAdjustment:
        if self.oversubscription.is_zero or self.oversubscription.is_negative:
            raise ValueError("oversubscription must be positive")
        expected = (self.margin_required - self.pre_trade_margin_available).quantized()
        if self.oversubscription != expected:
            raise ValueError(
                "oversubscription must equal margin_required minus "
                "pre_trade_margin_available"
            )
        return self
