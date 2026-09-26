"""OrderEvent: one durable record per order transition.

Invariant 11: the idempotency key is mandatory on every attempt and is stable
across retries, so attempt_number is recorded separately and never folded in.

Invariant 12: a timeout or acknowledgement never proves a fill. Fill quantities
are therefore only permitted in states the broker actually confirmed, and an
UNKNOWN event may not claim any fill at all.
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
from trading.domain.contracts.common import ContractRef
from trading.domain.enums import (
    ExecutionMode,
    OrderState,
    OrderType,
    ReasonCode,
    Side,
    TimeInForce,
)
from trading.domain.primitives import Price

__all__ = ["OrderCommand", "OrderEvent", "OrderIdentity"]

_STATES_ALLOWING_FILLS = frozenset({OrderState.PARTIAL, OrderState.FILLED})
_STATES_REQUIRING_A_REASON = frozenset(
    {OrderState.REJECTED, OrderState.EXPIRED, OrderState.UNKNOWN}
)


class OrderIdentity(StrictModel):
    """Every identifier needed to correlate one order across all three systems."""

    internal_order_id: NonEmptyStr
    client_order_id: NonEmptyStr
    idempotency_key: NonEmptyStr
    broker_order_id: NonEmptyStr | None = None
    intent_id: NonEmptyStr
    risk_decision_id: NonEmptyStr
    trade_id: NonEmptyStr
    correlation_id: NonEmptyStr
    experiment_id: NonEmptyStr
    execution_mode: ExecutionMode


class OrderCommand(StrictModel):
    """What was asked of the broker. Constructed only by Layer 2."""

    contract: ContractRef
    side: Side
    order_type: OrderType
    time_in_force: TimeInForce
    quantity_contracts: StrictInt = Field(gt=0)
    limit_price: Price | None = None
    trigger_price: Price | None = None

    @model_validator(mode="after")
    def _price_fields_match_the_order_type(self) -> OrderCommand:
        needs_limit = self.order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT}
        needs_trigger = self.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}
        if needs_limit and self.limit_price is None:
            raise ValueError(f"{self.order_type} requires a limit price")
        if not needs_limit and self.limit_price is not None:
            raise ValueError(f"{self.order_type} must not carry a limit price")
        if needs_trigger and self.trigger_price is None:
            raise ValueError(f"{self.order_type} requires a trigger price")
        if not needs_trigger and self.trigger_price is not None:
            raise ValueError(f"{self.order_type} must not carry a trigger price")
        return self


class OrderEvent(VersionedModel):
    """An immutable, auditable order state transition."""

    event_id: NonEmptyStr
    identity: OrderIdentity
    command: OrderCommand
    state: OrderState
    attempt_number: StrictInt = Field(ge=1)
    acknowledged_quantity: StrictInt = Field(default=0, ge=0)
    filled_quantity: StrictInt = Field(default=0, ge=0)
    average_fill_price: Price | None = None
    sent_at: UtcDatetime | None = None
    received_at: UtcDatetime
    broker_time: UtcDatetime | None = None
    raw_broker_status: NonEmptyStr | None = None
    raw_payload_ref: NonEmptyStr | None = None
    reason_code: ReasonCode | None = None
    reason_detail: str = ""
    strict_fill_verdict: ReasonCode | None = None
    reconciled: bool = False

    @model_validator(mode="after")
    def _quantities_and_state_agree(self) -> OrderEvent:
        requested = self.command.quantity_contracts
        if self.acknowledged_quantity > requested:
            raise ValueError(
                f"acknowledged {self.acknowledged_quantity} exceeds requested "
                f"{requested}"
            )
        if self.filled_quantity > requested:
            raise ValueError(
                f"filled {self.filled_quantity} exceeds requested {requested}"
            )

        if self.filled_quantity and self.state not in _STATES_ALLOWING_FILLS:
            raise ValueError(
                f"state {self.state} reports {self.filled_quantity} filled "
                "contracts. Only a broker-confirmed PARTIAL or FILLED may carry a "
                "fill; anything else would let a timeout imply execution "
                "(invariant 12)"
            )
        if self.state is OrderState.FILLED and self.filled_quantity != requested:
            raise ValueError(
                f"FILLED requires the full {requested} contracts, got "
                f"{self.filled_quantity}"
            )
        if (
            self.state is OrderState.PARTIAL
            and not 0 < self.filled_quantity < requested
        ):
            raise ValueError(
                f"PARTIAL requires a fill strictly between 0 and {requested}, got "
                f"{self.filled_quantity}"
            )
        if self.average_fill_price is not None and not self.filled_quantity:
            raise ValueError("an average fill price without a fill is meaningless")
        if self.filled_quantity and self.average_fill_price is None:
            raise ValueError("a fill must record the price it executed at")

        if self.state in _STATES_REQUIRING_A_REASON and self.reason_code is None:
            raise ValueError(f"state {self.state} requires a machine-readable reason")
        if self.state is not OrderState.CREATED and self.sent_at is None:
            raise ValueError(f"state {self.state} requires sent_at")
        if self.sent_at is not None and self.received_at < self.sent_at:
            raise ValueError("received_at precedes sent_at")
        # SUBMITTING is the one working state that legitimately predates a broker
        # identifier; every other implies the broker already knows the order.
        if (
            self.identity.broker_order_id is None
            and self.state.is_working
            and self.state is not OrderState.SUBMITTING
        ):
            raise ValueError(
                f"state {self.state} implies the broker knows this order, so a "
                "broker_order_id is required for reconciliation"
            )
        return self

    @property
    def outstanding_quantity(self) -> int:
        return self.command.quantity_contracts - self.filled_quantity
