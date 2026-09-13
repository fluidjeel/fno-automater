"""Portfolio truth models: broker-confirmed state Layer 2 sizes against.

Invariant 5: broker-reported positions, orders and funds are external truth.
Local PortfolioSnapshot is an operational mirror rebuilt from reconciliation.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictInt,
    StrictModel,
    UtcDatetime,
    VersionedModel,
)
from trading.domain.contracts.common import ContractRef, ExposureSnapshot, Versions
from trading.domain.enums import OrderState, Side, SystemState
from trading.domain.primitives import Money, Price

__all__ = [
    "PendingOrderSummary",
    "PortfolioSnapshot",
    "PortfolioView",
    "PositionRecord",
    "UnderlyingExposure",
]


class PositionRecord(StrictModel):
    """One broker-confirmed open position."""

    trade_id: NonEmptyStr
    strategy_id: NonEmptyStr
    contract: ContractRef
    side: Side
    quantity_contracts: StrictInt = Field(gt=0)
    average_price: Price
    unrealized_pnl: Money

    @property
    def signed_quantity(self) -> int:
        return (
            self.quantity_contracts
            if self.side is Side.BUY
            else -self.quantity_contracts
        )


class PendingOrderSummary(StrictModel):
    """A working order known to the broker or durably submitted locally."""

    internal_order_id: NonEmptyStr
    intent_id: NonEmptyStr
    contract: ContractRef
    side: Side
    quantity_contracts: StrictInt = Field(gt=0)
    state: OrderState

    @model_validator(mode="after")
    def _state_is_working(self) -> PendingOrderSummary:
        if not self.state.is_working:
            raise ValueError(
                f"pending order summary requires a working state, got {self.state}"
            )
        return self


class UnderlyingExposure(StrictModel):
    """Aggregated exposure for one underlying."""

    underlying: NonEmptyStr
    net_delta: StrictInt
    gross_notional: Money
    open_position_count: StrictInt = Field(ge=0)


class PortfolioSnapshot(VersionedModel):
    """Broker-confirmed portfolio state at a decision instant."""

    portfolio_snapshot_id: NonEmptyStr
    account_id: NonEmptyStr
    as_of: UtcDatetime
    exposure: ExposureSnapshot
    positions: tuple[PositionRecord, ...] = ()
    pending_orders: tuple[PendingOrderSummary, ...] = ()
    reserved_capital: Money
    underlying_exposure: tuple[UnderlyingExposure, ...] = ()
    reconciliation_ref: NonEmptyStr | None = None
    versions: Versions

    @model_validator(mode="after")
    def _exposure_and_reservation_share_currency(self) -> PortfolioSnapshot:
        if self.reserved_capital.currency is not self.exposure.equity.currency:
            raise ValueError(
                "reserved_capital must share currency with exposure figures"
            )
        if self.reserved_capital.is_negative:
            raise ValueError("reserved_capital must not be negative")
        return self

    @property
    def available_after_reservations(self) -> Money:
        return self.exposure.margin_available - self.reserved_capital


class PortfolioView(StrictModel):
    """Read-only portfolio slice exposed to Layer 3 strategies."""

    as_of: UtcDatetime
    open_trade_count: StrictInt = Field(ge=0)
    margin_available: Money
    realized_pnl_today: Money
    net_delta: StrictInt
    underlying_exposure: tuple[UnderlyingExposure, ...] = ()
    system_state: SystemState
    entries_permitted: bool

    @model_validator(mode="after")
    def _entries_follow_system_state(self) -> PortfolioView:
        if self.entries_permitted and not self.system_state.permits_new_exposure:
            raise ValueError(
                "entries_permitted cannot be true while system state blocks exposure"
            )
        currencies = {
            self.margin_available.currency,
            self.realized_pnl_today.currency,
        }
        for row in self.underlying_exposure:
            currencies.add(row.gross_notional.currency)
        if len(currencies) != 1:
            raise ValueError("portfolio view mixes currencies")
        return self

    @classmethod
    def from_snapshot(
        cls,
        snapshot: PortfolioSnapshot,
        *,
        system_state: SystemState,
        entries_permitted: bool,
    ) -> PortfolioView:
        """Derive the strategy-visible view from authoritative portfolio truth."""
        return cls(
            as_of=snapshot.as_of,
            open_trade_count=snapshot.exposure.open_trade_count,
            margin_available=snapshot.available_after_reservations,
            realized_pnl_today=snapshot.exposure.realized_pnl_today,
            net_delta=int(snapshot.exposure.net_delta),
            underlying_exposure=snapshot.underlying_exposure,
            system_state=system_state,
            entries_permitted=entries_permitted,
        )
