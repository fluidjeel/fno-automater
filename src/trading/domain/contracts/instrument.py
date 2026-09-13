"""InstrumentSpec: the authoritative contract specification Layer 2 sizes against.

DATA_SPEC.md requires reference data covering symbol mapping, contracts,
expiries, strikes, lot/tick sizes and sessions. Layer 2 cannot compute quantity,
margin or exposure without them, and a remembered lot size produces orders the
exchange rejects, so every field here originates from a dated provider snapshot
rather than a code constant.
"""

from __future__ import annotations

from datetime import date

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictInt,
    VersionedModel,
)
from trading.domain.enums import Exchange, InstrumentKind, OptionType

__all__ = ["InstrumentSpec"]


class InstrumentSpec(VersionedModel):
    """One tradable contract as published by the provider's instrument master."""

    trading_symbol: NonEmptyStr
    exchange: Exchange
    segment: NonEmptyStr
    underlying: NonEmptyStr
    instrument_kind: InstrumentKind
    provider_token: NonEmptyStr
    exchange_token: StrictInt

    # An index is quoted but not tradable, so its master row carries no lot.
    lot_size: StrictInt = Field(ge=0)
    tick_size: ExactDecimal = Field(gt=0)
    price_precision: StrictInt = Field(ge=0)
    freeze_quantity: StrictInt | None = Field(default=None, gt=0)

    expiry: date | None = None
    strike: ExactDecimal | None = Field(default=None, gt=0)
    option_type: OptionType | None = None

    trading_session: NonEmptyStr
    upper_price_band: ExactDecimal | None = Field(default=None, gt=0)
    lower_price_band: ExactDecimal | None = Field(default=None, gt=0)

    source: NonEmptyStr
    verified_at: date

    @model_validator(mode="after")
    def _derivative_fields_match_kind(self) -> InstrumentSpec:
        is_option = self.instrument_kind is InstrumentKind.OPTION
        if is_option and (self.strike is None or self.option_type is None):
            raise ValueError("an OPTION requires both strike and option_type")
        if not is_option and (self.strike is not None or self.option_type is not None):
            raise ValueError(
                f"{self.instrument_kind} must not carry strike or option_type"
            )
        is_derivative = self.instrument_kind in {
            InstrumentKind.OPTION,
            InstrumentKind.FUTURE,
        }
        if is_derivative and self.expiry is None:
            raise ValueError(f"{self.instrument_kind} requires an expiry")
        if is_derivative and self.lot_size <= 0:
            raise ValueError(
                f"{self.instrument_kind} requires a positive lot size; Layer 2 "
                "cannot size a tradable contract without one"
            )
        return self

    @model_validator(mode="after")
    def _price_bands_are_ordered(self) -> InstrumentSpec:
        if (
            self.upper_price_band is not None
            and self.lower_price_band is not None
            and self.lower_price_band > self.upper_price_band
        ):
            raise ValueError(
                f"lower band {self.lower_price_band} exceeds upper band "
                f"{self.upper_price_band}"
            )
        return self
