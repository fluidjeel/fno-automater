"""The four lifecycle machines from DOMAIN_CONTRACTS.md."""

from __future__ import annotations

from trading.domain.enums import (
    IntentState,
    OrderState,
    SystemState,
    TradeState,
    Trigger,
)
from trading.domain.state.machine import ANY_TRIGGER, StateMachine

__all__ = [
    "INTENT_MACHINE",
    "ORDER_MACHINE",
    "SYSTEM_MACHINE",
    "TRADE_MACHINE",
]

_RECONCILIATION_ONLY = frozenset({Trigger.RECONCILIATION})
_BROKER_ONLY = frozenset({Trigger.BROKER_EVENT})

# STARTING -> RECOVERY -> READY -> DEGRADED -> HALTED.
#
# Invariant 9: startup, restart and reconnect all begin in RECOVERY, so there is
# no edge from STARTING straight to READY. HALTED must pass back through RECOVERY
# before entries resume; it cannot jump to READY.
SYSTEM_MACHINE: StateMachine[SystemState] = StateMachine(
    "system",
    initial=SystemState.STARTING,
    states=frozenset(SystemState),
    terminal=frozenset(),
    edges={
        SystemState.STARTING: {
            SystemState.RECOVERY: frozenset({Trigger.STARTUP}),
            SystemState.HALTED: ANY_TRIGGER,
        },
        SystemState.RECOVERY: {
            SystemState.READY: frozenset({Trigger.RECONCILIATION}),
            SystemState.DEGRADED: ANY_TRIGGER,
            SystemState.HALTED: ANY_TRIGGER,
        },
        SystemState.READY: {
            SystemState.DEGRADED: ANY_TRIGGER,
            SystemState.HALTED: ANY_TRIGGER,
            SystemState.RECOVERY: ANY_TRIGGER,
        },
        SystemState.DEGRADED: {
            SystemState.READY: frozenset({Trigger.RECONCILIATION}),
            SystemState.RECOVERY: ANY_TRIGGER,
            SystemState.HALTED: ANY_TRIGGER,
        },
        SystemState.HALTED: {
            SystemState.RECOVERY: frozenset({Trigger.OPERATOR, Trigger.STARTUP}),
        },
    },
)

# CREATED -> VALIDATED -> APPROVED | RESIZED | DEFERRED | REJECTED | EXPIRED.
#
# Only Layer 2 produces the four decision states, so those edges are guarded by
# RISK_DECISION: a strategy cannot mark its own intent approved. A DEFERRED
# intent may be revalidated; an APPROVED or RESIZED one may still expire, which
# is what forces recalculation of a stale RiskDecision.
INTENT_MACHINE: StateMachine[IntentState] = StateMachine(
    "intent",
    initial=IntentState.CREATED,
    states=frozenset(IntentState),
    terminal=frozenset({IntentState.REJECTED, IntentState.EXPIRED}),
    edges={
        IntentState.CREATED: {
            IntentState.VALIDATED: ANY_TRIGGER,
            IntentState.REJECTED: ANY_TRIGGER,
            IntentState.EXPIRED: frozenset({Trigger.TIMEOUT, Trigger.SCHEDULER}),
        },
        IntentState.VALIDATED: {
            IntentState.APPROVED: frozenset({Trigger.RISK_DECISION}),
            IntentState.RESIZED: frozenset({Trigger.RISK_DECISION}),
            IntentState.DEFERRED: frozenset({Trigger.RISK_DECISION}),
            IntentState.REJECTED: frozenset({Trigger.RISK_DECISION}),
            IntentState.EXPIRED: frozenset({Trigger.TIMEOUT, Trigger.SCHEDULER}),
        },
        IntentState.DEFERRED: {
            IntentState.VALIDATED: ANY_TRIGGER,
            IntentState.EXPIRED: frozenset({Trigger.TIMEOUT, Trigger.SCHEDULER}),
            IntentState.REJECTED: ANY_TRIGGER,
        },
        IntentState.APPROVED: {
            IntentState.EXPIRED: frozenset({Trigger.TIMEOUT, Trigger.SCHEDULER}),
        },
        IntentState.RESIZED: {
            IntentState.EXPIRED: frozenset({Trigger.TIMEOUT, Trigger.SCHEDULER}),
        },
    },
)

# CREATED -> SUBMITTING -> ACKNOWLEDGED -> PARTIAL -> FILLED, plus the explicit
# failure and ambiguity states.
#
# The load-bearing rules, all from BROKER_SPEC.md and invariants 12 and 13:
#
#   - Only a broker event can advance an order to ACKNOWLEDGED, PARTIAL or
#     FILLED. A local timeout never proves a fill.
#   - SUBMITTING may only become UNKNOWN on TIMEOUT. An unknown submit outcome
#     is a distinct state, not an assumed rejection.
#   - UNKNOWN resolves exclusively through RECONCILIATION. There is no edge from
#     UNKNOWN on a local command, which is what prevents a blind retry from
#     creating duplicate exposure.
#   - CANCEL_PENDING can still reach FILLED: the cancel/fill race is real and
#     broker state wins.
ORDER_MACHINE: StateMachine[OrderState] = StateMachine(
    "order",
    initial=OrderState.CREATED,
    states=frozenset(OrderState),
    terminal=frozenset(
        {
            OrderState.FILLED,
            OrderState.REJECTED,
            OrderState.CANCELLED,
            OrderState.EXPIRED,
        }
    ),
    edges={
        OrderState.CREATED: {
            OrderState.SUBMITTING: frozenset({Trigger.LOCAL_COMMAND}),
            OrderState.REJECTED: ANY_TRIGGER,
            OrderState.EXPIRED: frozenset({Trigger.TIMEOUT, Trigger.SCHEDULER}),
        },
        OrderState.SUBMITTING: {
            OrderState.ACKNOWLEDGED: _BROKER_ONLY,
            OrderState.PARTIAL: _BROKER_ONLY,
            OrderState.FILLED: _BROKER_ONLY,
            OrderState.REJECTED: _BROKER_ONLY,
            OrderState.UNKNOWN: frozenset({Trigger.TIMEOUT}),
        },
        OrderState.ACKNOWLEDGED: {
            OrderState.PARTIAL: _BROKER_ONLY,
            OrderState.FILLED: _BROKER_ONLY,
            OrderState.CANCEL_PENDING: frozenset({Trigger.LOCAL_COMMAND}),
            OrderState.CANCELLED: _BROKER_ONLY,
            OrderState.REJECTED: _BROKER_ONLY,
            OrderState.EXPIRED: frozenset({Trigger.BROKER_EVENT, Trigger.SCHEDULER}),
            OrderState.UNKNOWN: frozenset({Trigger.TIMEOUT}),
        },
        OrderState.PARTIAL: {
            OrderState.FILLED: _BROKER_ONLY,
            OrderState.CANCEL_PENDING: frozenset({Trigger.LOCAL_COMMAND}),
            OrderState.CANCELLED: _BROKER_ONLY,
            OrderState.EXPIRED: frozenset({Trigger.BROKER_EVENT, Trigger.SCHEDULER}),
            OrderState.UNKNOWN: frozenset({Trigger.TIMEOUT}),
        },
        OrderState.CANCEL_PENDING: {
            # Broker state wins the cancel/fill race.
            OrderState.CANCELLED: _BROKER_ONLY,
            OrderState.FILLED: _BROKER_ONLY,
            OrderState.PARTIAL: _BROKER_ONLY,
            OrderState.UNKNOWN: frozenset({Trigger.TIMEOUT}),
        },
        OrderState.UNKNOWN: {
            OrderState.ACKNOWLEDGED: _RECONCILIATION_ONLY,
            OrderState.PARTIAL: _RECONCILIATION_ONLY,
            OrderState.FILLED: _RECONCILIATION_ONLY,
            OrderState.REJECTED: _RECONCILIATION_ONLY,
            OrderState.CANCELLED: _RECONCILIATION_ONLY,
            OrderState.EXPIRED: _RECONCILIATION_ONLY,
        },
    },
)

# PENDING_ENTRY -> OPENING -> OPEN -> EXIT_PENDING -> CLOSING -> CLOSED, plus
# REPAIR_REQUIRED.
#
# A multi-leg partial fill sends the trade to REPAIR_REQUIRED from any live
# state. Invariant 15 and the runbook require a pre-approved unwind policy, so
# REPAIR_REQUIRED leads only to CLOSING or, once repaired, back to OPEN. It
# cannot short-circuit to CLOSED, because closing must go through orders.
TRADE_MACHINE: StateMachine[TradeState] = StateMachine(
    "trade",
    initial=TradeState.PENDING_ENTRY,
    states=frozenset(TradeState),
    terminal=frozenset({TradeState.CLOSED}),
    edges={
        TradeState.PENDING_ENTRY: {
            TradeState.OPENING: frozenset({Trigger.LOCAL_COMMAND}),
            TradeState.CLOSED: frozenset(
                {Trigger.TIMEOUT, Trigger.SCHEDULER, Trigger.OPERATOR}
            ),
            TradeState.REPAIR_REQUIRED: ANY_TRIGGER,
        },
        TradeState.OPENING: {
            TradeState.OPEN: frozenset({Trigger.BROKER_EVENT, Trigger.RECONCILIATION}),
            TradeState.CLOSING: ANY_TRIGGER,
            TradeState.REPAIR_REQUIRED: ANY_TRIGGER,
        },
        TradeState.OPEN: {
            TradeState.EXIT_PENDING: ANY_TRIGGER,
            TradeState.CLOSING: ANY_TRIGGER,
            TradeState.REPAIR_REQUIRED: ANY_TRIGGER,
        },
        TradeState.EXIT_PENDING: {
            TradeState.CLOSING: frozenset({Trigger.LOCAL_COMMAND}),
            TradeState.OPEN: frozenset({Trigger.RECONCILIATION}),
            TradeState.REPAIR_REQUIRED: ANY_TRIGGER,
        },
        TradeState.CLOSING: {
            TradeState.CLOSED: frozenset(
                {Trigger.BROKER_EVENT, Trigger.RECONCILIATION}
            ),
            TradeState.OPEN: frozenset({Trigger.RECONCILIATION}),
            TradeState.REPAIR_REQUIRED: ANY_TRIGGER,
        },
        TradeState.REPAIR_REQUIRED: {
            TradeState.CLOSING: ANY_TRIGGER,
            TradeState.OPEN: frozenset({Trigger.RECONCILIATION}),
        },
    },
)
