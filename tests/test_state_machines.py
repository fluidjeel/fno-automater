"""State machine boundaries.

Invariant 9: startup, restart and reconnect begin in RECOVERY.
Invariant 12: a timeout or acknowledgement never proves a fill.
Invariant 13: an unknown submit outcome blocks replacement until reconciliation.
Invariant 15: a partial multi-leg fill follows a pre-approved repair policy.
DOMAIN_CONTRACTS.md: illegal transitions fail closed and emit audit evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum, unique
from types import MappingProxyType
from typing import Any

import pytest

from trading.domain.enums import (
    IntentState,
    OrderState,
    ReasonCode,
    SystemState,
    TradeState,
    Trigger,
)
from trading.domain.state import (
    INTENT_MACHINE,
    ORDER_MACHINE,
    RESERVATION_MACHINE,
    SYSTEM_MACHINE,
    TRADE_MACHINE,
    IllegalTransitionError,
    StateMachine,
)
from trading.domain.state.machine import ANY_TRIGGER

AT = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)

ALL_MACHINES: tuple[StateMachine[Any], ...] = (
    SYSTEM_MACHINE,
    INTENT_MACHINE,
    ORDER_MACHINE,
    TRADE_MACHINE,
    RESERVATION_MACHINE,
)


def _cross_product(
    machine: StateMachine[Any],
) -> list[tuple[StrEnum, StrEnum, Trigger]]:
    return [
        (source, target, trigger)
        for source in sorted(machine.states)
        for target in sorted(machine.states)
        for trigger in sorted(Trigger)
    ]


class TestExhaustiveCrossProduct:
    """Every state pair under every trigger is either allowed or rejected."""

    @pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.name)
    def test_every_pair_is_classified(self, machine: StateMachine[Any]) -> None:
        for source, target, trigger in _cross_product(machine):
            if machine.can(source, target, trigger):
                record = machine.transition(source, target, trigger=trigger, at=AT)
                assert record.allowed
                assert record.reason_code is ReasonCode.OK
            else:
                with pytest.raises(IllegalTransitionError) as caught:
                    machine.transition(source, target, trigger=trigger, at=AT)
                record = caught.value.record
                assert record.allowed is False
                assert record.reason_code is ReasonCode.ILLEGAL_STATE_TRANSITION
                assert record.source is source
                assert record.target is target
                assert record.trigger is trigger

    @pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.name)
    def test_self_transitions_are_not_silently_allowed(
        self, machine: StateMachine[Any]
    ) -> None:
        """A no-op transition must be an explicit decision, not an accident."""
        for state in machine.states:
            assert state not in machine.allowed_targets(state)

    @pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.name)
    def test_every_non_initial_state_is_reachable(
        self, machine: StateMachine[Any]
    ) -> None:
        """A declared but unreachable state would be dead, untestable code."""
        reached = {machine.initial}
        frontier = [machine.initial]
        while frontier:
            for target in machine.allowed_targets(frontier.pop()):
                if target not in reached:
                    reached.add(target)
                    frontier.append(target)
        assert reached == machine.states

    @pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.name)
    def test_terminal_states_have_no_exit(self, machine: StateMachine[Any]) -> None:
        for state in machine.terminal:
            assert machine.allowed_targets(state) == frozenset()

    @pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.name)
    def test_undeclared_state_is_rejected_loudly(
        self, machine: StateMachine[Any]
    ) -> None:
        @unique
        class Foreign(StrEnum):
            OTHER = "OTHER"

        with pytest.raises(ValueError, match="not declared"):
            machine.transition(
                Foreign.OTHER, machine.initial, trigger=Trigger.OPERATOR, at=AT
            )

    @pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.name)
    def test_records_require_aware_timestamps(self, machine: StateMachine[Any]) -> None:
        target = next(iter(machine.allowed_targets(machine.initial)))
        trigger = next(
            iter(
                machine.allowed_triggers(machine.initial, target) or {Trigger.OPERATOR}
            )
        )
        with pytest.raises(ValueError, match="naive"):
            machine.transition(
                machine.initial,
                target,
                trigger=trigger,
                at=datetime(2026, 9, 14, 4, 0),
            )


class TestSystemMachine:
    def test_startup_cannot_skip_recovery(self) -> None:
        """Invariant 9: entries wait for reconciliation after every start."""
        assert not SYSTEM_MACHINE.can(
            SystemState.STARTING, SystemState.READY, Trigger.STARTUP
        )
        assert SYSTEM_MACHINE.can(
            SystemState.STARTING, SystemState.RECOVERY, Trigger.STARTUP
        )

    def test_only_reconciliation_opens_the_system_for_entries(self) -> None:
        assert SYSTEM_MACHINE.allowed_triggers(
            SystemState.RECOVERY, SystemState.READY
        ) == frozenset({Trigger.RECONCILIATION})
        assert not SYSTEM_MACHINE.can(
            SystemState.RECOVERY, SystemState.READY, Trigger.OPERATOR
        )

    def test_halted_must_pass_back_through_recovery(self) -> None:
        assert SYSTEM_MACHINE.allowed_targets(SystemState.HALTED) == frozenset(
            {SystemState.RECOVERY}
        )

    def test_only_ready_permits_new_exposure(self) -> None:
        permitting = {s for s in SystemState if s.permits_new_exposure}
        assert permitting == {SystemState.READY}


class TestIntentMachine:
    def test_only_a_risk_decision_can_approve_an_intent(self) -> None:
        """A strategy cannot mark its own intent approved."""
        for outcome in (
            IntentState.APPROVED,
            IntentState.RESIZED,
            IntentState.DEFERRED,
            IntentState.REJECTED,
        ):
            assert INTENT_MACHINE.allowed_triggers(
                IntentState.VALIDATED, outcome
            ) == frozenset({Trigger.RISK_DECISION})
            assert not INTENT_MACHINE.can(
                IntentState.VALIDATED, outcome, Trigger.STRATEGY
            )

    def test_created_cannot_be_approved_without_validation(self) -> None:
        assert not INTENT_MACHINE.can(
            IntentState.CREATED, IntentState.APPROVED, Trigger.RISK_DECISION
        )

    def test_approved_intent_can_still_expire(self) -> None:
        """An expired approval must be recalculated, never consumed."""
        assert INTENT_MACHINE.can(
            IntentState.APPROVED, IntentState.EXPIRED, Trigger.TIMEOUT
        )

    def test_expired_intent_cannot_be_revived(self) -> None:
        assert INTENT_MACHINE.allowed_targets(IntentState.EXPIRED) == frozenset()


class TestOrderMachine:
    def test_a_timeout_never_produces_a_fill(self) -> None:
        """Invariant 12: timeout and acknowledgement do not prove a fill."""
        for source in (
            OrderState.SUBMITTING,
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIAL,
            OrderState.CANCEL_PENDING,
        ):
            assert not ORDER_MACHINE.can(source, OrderState.FILLED, Trigger.TIMEOUT)

    def test_only_the_broker_can_report_a_fill(self) -> None:
        for source in (
            OrderState.SUBMITTING,
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIAL,
            OrderState.CANCEL_PENDING,
        ):
            assert ORDER_MACHINE.allowed_triggers(
                source, OrderState.FILLED
            ) == frozenset({Trigger.BROKER_EVENT})

    def test_unknown_is_reachable_only_by_timeout(self) -> None:
        sources = {
            source
            for source in OrderState
            if ORDER_MACHINE.can(source, OrderState.UNKNOWN, Trigger.TIMEOUT)
        }
        assert sources == {
            OrderState.SUBMITTING,
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIAL,
            OrderState.CANCEL_PENDING,
        }
        for source in sources:
            for trigger in Trigger:
                if trigger is not Trigger.TIMEOUT:
                    assert not ORDER_MACHINE.can(source, OrderState.UNKNOWN, trigger)

    def test_unknown_resolves_only_through_reconciliation(self) -> None:
        """Invariant 13: no blind retry can escape an ambiguous submit."""
        targets = ORDER_MACHINE.allowed_targets(OrderState.UNKNOWN)
        assert targets
        for target in targets:
            assert ORDER_MACHINE.allowed_triggers(
                OrderState.UNKNOWN, target
            ) == frozenset({Trigger.RECONCILIATION})
        for target in OrderState:
            assert not ORDER_MACHINE.can(
                OrderState.UNKNOWN, target, Trigger.LOCAL_COMMAND
            )

    def test_unknown_cannot_be_resubmitted(self) -> None:
        assert OrderState.SUBMITTING not in ORDER_MACHINE.allowed_targets(
            OrderState.UNKNOWN
        )

    def test_cancel_pending_can_still_fill(self) -> None:
        """The cancel/fill race is real, and broker state wins it."""
        assert ORDER_MACHINE.can(
            OrderState.CANCEL_PENDING, OrderState.FILLED, Trigger.BROKER_EVENT
        )

    def test_submission_requires_a_local_command(self) -> None:
        assert ORDER_MACHINE.allowed_triggers(
            OrderState.CREATED, OrderState.SUBMITTING
        ) == frozenset({Trigger.LOCAL_COMMAND})

    def test_terminal_orders_are_final(self) -> None:
        for state in OrderState:
            if state.is_terminal:
                assert ORDER_MACHINE.allowed_targets(state) == frozenset()

    def test_working_and_terminal_are_disjoint(self) -> None:
        for state in OrderState:
            assert not (state.is_working and state.is_terminal)


class TestTradeMachine:
    def test_partial_fill_routes_to_repair_from_every_live_state(self) -> None:
        """Invariant 15: a broken multi-leg position has one destination."""
        for source in (
            TradeState.PENDING_ENTRY,
            TradeState.OPENING,
            TradeState.OPEN,
            TradeState.EXIT_PENDING,
            TradeState.CLOSING,
        ):
            assert TradeState.REPAIR_REQUIRED in TRADE_MACHINE.allowed_targets(source)

    def test_repair_cannot_short_circuit_to_closed(self) -> None:
        """Closing goes through orders, so repair cannot declare itself done."""
        assert TradeState.CLOSED not in TRADE_MACHINE.allowed_targets(
            TradeState.REPAIR_REQUIRED
        )

    def test_open_trade_cannot_jump_to_closed(self) -> None:
        assert TradeState.CLOSED not in TRADE_MACHINE.allowed_targets(TradeState.OPEN)

    def test_only_the_broker_or_reconciliation_confirms_a_close(self) -> None:
        assert TRADE_MACHINE.allowed_triggers(
            TradeState.CLOSING, TradeState.CLOSED
        ) == frozenset({Trigger.BROKER_EVENT, Trigger.RECONCILIATION})

    def test_every_state_needing_protection_is_non_terminal(self) -> None:
        """Invariant 16: a position that needs protection is still manageable."""
        for state in TradeState:
            if state.requires_protective_coverage:
                assert state not in TRADE_MACHINE.terminal

    def test_closed_needs_no_protection(self) -> None:
        assert not TradeState.CLOSED.requires_protective_coverage


class TestTableConstruction:
    def test_undeclared_state_in_table_is_rejected(self) -> None:
        @unique
        class Two(StrEnum):
            A = "A"
            B = "B"

        @unique
        class Other(StrEnum):
            C = "C"

        with pytest.raises(ValueError, match="outside the declared set"):
            StateMachine[Any](
                "bad",
                initial=Two.A,
                states=frozenset(Two),
                terminal=frozenset(),
                edges={Two.A: {Other.C: ANY_TRIGGER}},
            )

    def test_terminal_state_with_an_exit_is_rejected(self) -> None:
        @unique
        class Two(StrEnum):
            A = "A"
            B = "B"

        with pytest.raises(ValueError, match="have outgoing edges"):
            StateMachine(
                "bad",
                initial=Two.A,
                states=frozenset(Two),
                terminal=frozenset({Two.B}),
                edges={Two.A: {Two.B: ANY_TRIGGER}, Two.B: {Two.A: ANY_TRIGGER}},
            )

    def test_table_is_immutable_after_construction(self) -> None:
        """A caller cannot widen a machine at runtime to legalise a transition."""
        assert isinstance(ORDER_MACHINE.allowed_targets(OrderState.CREATED), frozenset)
        edges = ORDER_MACHINE.outgoing(OrderState.CREATED)
        assert isinstance(edges, MappingProxyType)
        with pytest.raises(TypeError):
            edges[OrderState.FILLED] = ANY_TRIGGER  # type: ignore[index]
