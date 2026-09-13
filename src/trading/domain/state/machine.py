"""Generic guarded transition table.

DOMAIN_CONTRACTS.md: "Illegal transitions fail closed and emit audit evidence."
Both halves matter. Raising alone loses the evidence; recording alone lets the
system continue from a state it never legally reached. So every attempt produces
a TransitionRecord, and a rejected attempt raises with that record attached for
the caller to persist.

Edges are guarded by Trigger. That is what lets OrderState.UNKNOWN be reachable
while only reconciliation may resolve it, satisfying invariant 13.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from trading.domain.clock import ensure_utc
from trading.domain.enums import ReasonCode, Trigger

__all__ = [
    "ANY_TRIGGER",
    "IllegalTransitionError",
    "StateMachine",
    "TransitionRecord",
]

S = TypeVar("S", bound=StrEnum)

# An empty trigger set on an edge means "any trigger". Spelled out so the
# transition tables stay readable.
ANY_TRIGGER: frozenset[Trigger] = frozenset()

_NO_EDGES: Mapping[Any, frozenset[Trigger]] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class TransitionRecord(Generic[S]):
    """Durable evidence of one transition attempt, legal or not."""

    machine: str
    source: S
    target: S
    trigger: Trigger
    at: datetime
    allowed: bool
    reason_code: ReasonCode
    detail: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "at", ensure_utc(self.at, "TransitionRecord.at"))


class IllegalTransitionError(RuntimeError):
    """Raised when a transition is not in the machine's table.

    Carries the audit record so the caller persists the same evidence it would
    have persisted for a legal transition.
    """

    def __init__(self, record: TransitionRecord[StrEnum]) -> None:
        super().__init__(record.detail)
        self.record = record


class StateMachine(Generic[S]):
    """An immutable guarded transition table for one lifecycle."""

    __slots__ = ("_edges", "_initial", "_name", "_states", "_terminal")

    def __init__(
        self,
        name: str,
        *,
        initial: S,
        states: frozenset[S],
        edges: dict[S, dict[S, frozenset[Trigger]]],
        terminal: frozenset[S],
    ) -> None:
        unknown_sources = set(edges) - states
        unknown_targets = {t for targets in edges.values() for t in targets} - states
        if unknown_sources or unknown_targets:
            raise ValueError(
                f"{name}: table references states outside the declared set: "
                f"{sorted(unknown_sources | unknown_targets)}"
            )
        if initial not in states:
            raise ValueError(f"{name}: initial state {initial} is not declared")
        escaping = {s for s in terminal if edges.get(s)}
        if escaping:
            raise ValueError(
                f"{name}: terminal states {sorted(escaping)} have outgoing edges"
            )
        self._name = name
        self._initial = initial
        self._states = states
        self._terminal = terminal
        self._edges: Mapping[S, Mapping[S, frozenset[Trigger]]] = MappingProxyType(
            {
                source: MappingProxyType(dict(targets))
                for source, targets in edges.items()
            }
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def initial(self) -> S:
        return self._initial

    @property
    def states(self) -> frozenset[S]:
        return self._states

    @property
    def terminal(self) -> frozenset[S]:
        return self._terminal

    def outgoing(self, source: S) -> Mapping[S, frozenset[Trigger]]:
        """Read-only view of the edges leaving a state, keyed by target."""
        return self._edges.get(source, _NO_EDGES)

    def allowed_targets(self, source: S) -> frozenset[S]:
        return frozenset(self.outgoing(source))

    def allowed_triggers(self, source: S, target: S) -> frozenset[Trigger]:
        """Triggers permitted on an edge. Empty means the edge is unguarded."""
        return self.outgoing(source).get(target, ANY_TRIGGER)

    def can(self, source: S, target: S, trigger: Trigger) -> bool:
        targets = self.outgoing(source)
        if target not in targets:
            return False
        permitted = targets[target]
        return not permitted or trigger in permitted

    def transition(
        self,
        source: S,
        target: S,
        *,
        trigger: Trigger,
        at: datetime,
    ) -> TransitionRecord[S]:
        """Validate a transition and return its audit record.

        Raises IllegalTransitionError, with the record attached, when the edge is
        absent or the trigger is not permitted on it.
        """
        for state, label in ((source, "source"), (target, "target")):
            if state not in self._states:
                raise ValueError(f"{self._name}: {label} {state!r} is not declared")

        if self.can(source, target, trigger):
            return TransitionRecord(
                machine=self._name,
                source=source,
                target=target,
                trigger=trigger,
                at=at,
                allowed=True,
                reason_code=ReasonCode.OK,
                detail=f"{self._name}: {source} -> {target} on {trigger}",
            )

        targets = self.outgoing(source)
        if target in targets:
            detail = (
                f"{self._name}: {source} -> {target} is not permitted on trigger "
                f"{trigger}; allowed triggers are {sorted(targets[target])}"
            )
        else:
            detail = (
                f"{self._name}: {source} -> {target} is not a legal transition; "
                f"reachable targets are {sorted(self.allowed_targets(source))}"
            )
        raise IllegalTransitionError(
            TransitionRecord(
                machine=self._name,
                source=source,
                target=target,
                trigger=trigger,
                at=at,
                allowed=False,
                reason_code=ReasonCode.ILLEGAL_STATE_TRANSITION,
                detail=detail,
            )
        )
