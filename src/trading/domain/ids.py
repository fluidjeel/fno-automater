"""Identity as an injected port, plus the idempotency key derivation.

Invariant 11: one logical order uses one stable idempotency key across retries.
Invariant 13: an unknown submit outcome blocks replacement until reconciliation.

Together these mean the key must be a pure function of what the order *is*, not
of when or how many times we tried to send it. derive_idempotency_key therefore
deliberately excludes attempt number, timestamps and randomness: a retry of the
same logical order recomputes a byte-identical key, in this process or the next,
so the broker can reject the duplicate.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from trading.domain.clock import ensure_utc

__all__ = [
    "IdError",
    "IdFactory",
    "SequentialIdFactory",
    "derive_idempotency_key",
]

_KEY_BYTES = 16
_FIELD_SEPARATOR = "\x1f"  # ASCII unit separator: cannot occur in an identifier


class IdError(ValueError):
    """Raised for malformed identifier components."""


@runtime_checkable
class IdFactory(Protocol):
    """Source of unique, lexicographically sortable identifiers."""

    def new_id(self, prefix: str) -> str:
        """A fresh identifier, prefixed to make its kind readable in logs."""
        ...


def _validate_component(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise IdError(f"{name} must be a str, got {type(value).__name__}")
    if not value:
        raise IdError(f"{name} must not be empty")
    if _FIELD_SEPARATOR in value:
        raise IdError(f"{name} must not contain the field separator")
    return value


def derive_idempotency_key(
    *,
    account_id: str,
    strategy_id: str,
    strategy_version: str,
    intent_id: str,
    leg_id: str,
    side: str,
    quantity_contracts: int,
) -> str:
    """Stable key identifying one logical order.

    Excludes attempt number, timestamp and randomness by design: a retry must
    produce the same key. Includes account so the same intent replayed against a
    different environment cannot collide with a live order.

    Quantity is included because a resized order is a different logical order
    and must not be deduplicated against the original.
    """
    # Exact int only: bool is an int subclass, so True must not mean one contract.
    if type(quantity_contracts) is not int:
        raise IdError(
            "quantity_contracts must be an int, got "
            f"{type(quantity_contracts).__name__}"
        )
    if quantity_contracts == 0:
        raise IdError("quantity_contracts must be non-zero for a real order")

    components = (
        _validate_component(account_id, "account_id"),
        _validate_component(strategy_id, "strategy_id"),
        _validate_component(strategy_version, "strategy_version"),
        _validate_component(intent_id, "intent_id"),
        _validate_component(leg_id, "leg_id"),
        _validate_component(side, "side"),
        str(quantity_contracts),
    )
    payload = _FIELD_SEPARATOR.join(components).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=_KEY_BYTES).hexdigest()


@dataclass(slots=True)
class SequentialIdFactory:
    """Deterministic identifier factory: clock instant plus a monotonic counter.

    Deterministic by construction, so replay of the same event sequence yields
    the same identifiers. Uniqueness comes from the counter rather than
    randomness, which is why it is safe inside the pure domain layer.
    """

    instant: datetime
    counter: int = 0
    _width: int = field(default=8, repr=False)

    def __post_init__(self) -> None:
        self.instant = ensure_utc(self.instant, "SequentialIdFactory.instant")
        if self.counter < 0:
            raise IdError("counter must not be negative")

    def new_id(self, prefix: str) -> str:
        _validate_component(prefix, "prefix")
        self.counter += 1
        stamp = f"{int(self.instant.timestamp() * 1_000_000):016d}"
        return f"{prefix}-{stamp}-{self.counter:0{self._width}d}"
