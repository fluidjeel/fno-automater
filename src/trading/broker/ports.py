"""Typed broker ports. Adapters translate canonical commands and broker truth.

Invariant 5: broker-reported funds, positions and orders are external truth.
Invariant 11: submit uses a stable idempotency key across retries.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field

from trading.domain.contracts.base import (
    NonEmptyStr,
    StrictBool,
    StrictInt,
    StrictModel,
    UtcDatetime,
)
from trading.domain.contracts.common import ContractRef
from trading.domain.contracts.order import OrderEvent
from trading.domain.contracts.order_plan import PlannedOrder
from trading.domain.contracts.portfolio import (
    PendingOrderSummary,
    PositionRecord,
)
from trading.domain.enums import Side
from trading.domain.primitives import Money

__all__ = [
    "BrokerError",
    "BrokerFunds",
    "BrokerPort",
    "BrokerSubmitRequest",
    "DuplicateBrokerOrderError",
    "MarginPreviewLeg",
    "MarginPreviewPort",
    "MarginPreviewRequest",
    "MarginPreviewResult",
]


class BrokerError(Exception):
    """Base error for broker adapter failures."""


class DuplicateBrokerOrderError(BrokerError):
    """Invariant 11: duplicate idempotency key at the broker."""

    def __init__(self, idempotency_key: str, existing_event: OrderEvent) -> None:
        self.idempotency_key = idempotency_key
        self.existing_event = existing_event
        super().__init__(f"DUPLICATE_IDEMPOTENCY_KEY: {idempotency_key}")


class BrokerFunds(StrictModel):
    """Broker-confirmed account funds and margin."""

    account_id: NonEmptyStr
    as_of: UtcDatetime
    equity: Money
    margin_used: Money
    margin_available: Money


class BrokerSubmitRequest(StrictModel):
    """Canonical submit payload constructed only by Layer 2 OMS."""

    account_id: NonEmptyStr
    strategy_id: NonEmptyStr
    order: PlannedOrder
    attempt_number: StrictInt = Field(default=1, ge=1)


class MarginPreviewLeg(StrictModel):
    """One leg in a broker margin preview request."""

    contract: ContractRef
    side: Side
    quantity_contracts: StrictInt = Field(gt=0)


class MarginPreviewRequest(StrictModel):
    """Preflight margin query before risk approval."""

    request_id: NonEmptyStr
    account_id: NonEmptyStr
    legs: tuple[MarginPreviewLeg, ...]


class MarginPreviewResult(StrictModel):
    """Broker margin preview response."""

    request_id: NonEmptyStr
    as_of: UtcDatetime
    margin_required: Money
    margin_available_after: Money
    confirmed: StrictBool


@runtime_checkable
class BrokerPort(Protocol):
    """Order and portfolio query surface for broker adapters."""

    def submit(self, request: BrokerSubmitRequest) -> OrderEvent:
        """Submit one planned order and return the broker-confirmed transition."""
        ...

    def cancel(self, internal_order_id: str) -> OrderEvent:
        """Cancel a working order by internal order id."""
        ...

    def get_order(self, internal_order_id: str) -> OrderEvent | None:
        """Return the latest known event for one order."""
        ...

    def list_orders(self) -> tuple[OrderEvent, ...]:
        """Return every order event retained by the adapter."""
        ...

    def get_positions(self) -> tuple[PositionRecord, ...]:
        """Return broker-confirmed open positions."""
        ...

    def get_pending_orders(self) -> tuple[PendingOrderSummary, ...]:
        """Return working orders still open at the broker."""
        ...

    def get_funds(self) -> BrokerFunds:
        """Return broker-confirmed funds and margin."""
        ...


@runtime_checkable
class MarginPreviewPort(Protocol):
    """Margin preflight surface for sizing and risk."""

    def preview_margin(self, request: MarginPreviewRequest) -> MarginPreviewResult:
        """Return broker margin requirements for a proposed order set."""
        ...
