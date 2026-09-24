"""Durable per-fill charge breakdown keyed by fill identity."""

from __future__ import annotations

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.enums import Side
from trading.domain.primitives import Money

__all__ = ["ChargeCalculationInputs", "FillChargeComponents", "FillChargeRecord"]


class FillChargeComponents(StrictModel):
    """Statutory charge lines for one confirmed fill."""

    brokerage: Money
    exchange_fees: Money
    taxes: Money
    gst: Money
    stamp_duty: Money

    @property
    def total(self) -> Money:
        currency = self.brokerage.currency
        amount = (
            self.brokerage.amount
            + self.exchange_fees.amount
            + self.taxes.amount
            + self.gst.amount
            + self.stamp_duty.amount
        )
        return Money.of(amount, currency)


class ChargeCalculationInputs(StrictModel):
    """Frozen inputs used to compute one fill's charges."""

    trade_id: NonEmptyStr
    order_event_id: NonEmptyStr
    side: Side
    filled_quantity: StrictInt = Field(gt=0)
    contracts_per_lot: StrictInt = Field(gt=0)
    premium_per_contract: NonEmptyStr
    turnover: NonEmptyStr


class FillChargeRecord(VersionedModel):
    """One durable charge row per fill identity (order idempotency key)."""

    fill_idempotency_key: NonEmptyStr
    trade_id: NonEmptyStr
    order_event_id: NonEmptyStr
    policy_version: NonEmptyStr
    components: FillChargeComponents
    total_charges: Money
    inputs: ChargeCalculationInputs
    recorded_at: UtcDatetime

    @model_validator(mode="after")
    def _total_matches_components(self) -> FillChargeRecord:
        if self.total_charges != self.components.total:
            raise ValueError("total_charges must equal the sum of charge components")
        return self
