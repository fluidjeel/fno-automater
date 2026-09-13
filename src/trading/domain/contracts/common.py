"""Shared value structures used by more than one contract."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    ExactDecimal,
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    UtcDatetime,
)
from trading.domain.enums import (
    AssetClass,
    DataQuality,
    Exchange,
    InstrumentKind,
    OptionType,
    ReasonCode,
)
from trading.domain.primitives import Money

__all__ = [
    "ContractRef",
    "DataQualityReport",
    "ExposureSnapshot",
    "Lineage",
    "Versions",
]


class ContractRef(StrictModel):
    """Identity of one tradable contract, resolved from the instrument master."""

    exchange: Exchange
    symbol: NonEmptyStr
    instrument_kind: InstrumentKind
    asset_class: AssetClass
    underlying: NonEmptyStr
    broker_token: NonEmptyStr | None = None
    expiry: date | None = None
    strike: ExactDecimal | None = None
    option_type: OptionType | None = None

    @model_validator(mode="after")
    def _derivative_fields_match_kind(self) -> ContractRef:
        is_option = self.instrument_kind is InstrumentKind.OPTION
        if is_option and (self.strike is None or self.option_type is None):
            raise ValueError("an OPTION requires both strike and option_type")
        if not is_option and (self.strike is not None or self.option_type is not None):
            raise ValueError(
                f"{self.instrument_kind} must not carry strike or option_type"
            )
        if (
            self.instrument_kind in {InstrumentKind.OPTION, InstrumentKind.FUTURE}
            and self.expiry is None
        ):
            raise ValueError(f"{self.instrument_kind} requires an expiry")
        if self.strike is not None and self.strike <= 0:
            raise ValueError("strike must be positive")
        return self


class Versions(StrictModel):
    """Everything needed to reproduce a decision (invariant 21)."""

    code_version: NonEmptyStr
    config_version: NonEmptyStr
    config_checksum: NonEmptyStr


class Lineage(StrictModel):
    """Where a value came from, so a decision can be traced to raw evidence."""

    provider: NonEmptyStr
    source_ids: tuple[NonEmptyStr, ...] = ()
    raw_event_refs: tuple[NonEmptyStr, ...] = ()
    normalization_version: NonEmptyStr
    versions: Versions


class DataQualityReport(StrictModel):
    """DATA_SPEC.md quality gate outcome for one snapshot."""

    state: DataQuality
    age_ms: StrictInt = Field(ge=0)
    gap_count: StrictInt = Field(default=0, ge=0)
    warmup_complete: StrictBool
    clock_drift_ms: StrictInt = Field(default=0)
    source_status: NonEmptyStr
    reason_codes: tuple[ReasonCode, ...] = ()

    @model_validator(mode="after")
    def _non_valid_states_explain_themselves(self) -> DataQualityReport:
        """A degraded snapshot without a reason code is unroutable by alerting."""
        if self.state is not DataQuality.VALID and not self.reason_codes:
            raise ValueError(f"state {self.state} requires at least one reason code")
        if self.state is DataQuality.VALID and not self.warmup_complete:
            raise ValueError("a VALID snapshot cannot have incomplete warm-up")
        return self

    @property
    def permits_new_exposure(self) -> bool:
        """Invariant 6. DEGRADED is permitted only if the strategy tolerates it."""
        return not self.state.blocks_new_exposure


class ExposureSnapshot(StrictModel):
    """Portfolio state at a point in time, before or projected after a fill."""

    as_of: UtcDatetime
    equity: Money
    margin_used: Money
    margin_available: Money
    open_trade_count: StrictInt = Field(ge=0)
    net_delta: ExactDecimal = Decimal(0)
    gross_notional: Money
    realized_pnl_today: Money
    unrealized_pnl: Money

    @model_validator(mode="after")
    def _amounts_share_one_currency(self) -> ExposureSnapshot:
        currencies = {
            self.equity.currency,
            self.margin_used.currency,
            self.margin_available.currency,
            self.gross_notional.currency,
            self.realized_pnl_today.currency,
            self.unrealized_pnl.currency,
        }
        if len(currencies) != 1:
            raise ValueError(
                f"exposure mixes currencies {sorted(c.value for c in currencies)}; "
                "convert explicitly with a dated rate before aggregating"
            )
        if self.margin_used.is_negative or self.margin_available.is_negative:
            raise ValueError("margin figures must not be negative")
        return self
