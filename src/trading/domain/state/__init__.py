"""State machines. Illegal transitions fail closed and emit audit evidence."""

from trading.domain.state.machine import (
    IllegalTransitionError,
    StateMachine,
    TransitionRecord,
)
from trading.domain.state.machines import (
    INTENT_MACHINE,
    ORDER_MACHINE,
    SYSTEM_MACHINE,
    TRADE_MACHINE,
)

__all__ = [
    "INTENT_MACHINE",
    "ORDER_MACHINE",
    "SYSTEM_MACHINE",
    "TRADE_MACHINE",
    "IllegalTransitionError",
    "StateMachine",
    "TransitionRecord",
]
