"""Atomic capital reservation (L2-005).

Invariant 14: capital is reserved before submission; concurrent reserves cannot
overspend the available margin budget.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

import tests.factories as f
from trading.domain.clock import FrozenClock
from trading.domain.contracts.reservation import CapitalReservation
from trading.domain.enums import ReasonCode, ReservationState, Trigger
from trading.domain.ids import SequentialIdFactory
from trading.portfolio.snapshot import sum_active_reservations
from trading.risk import (
    CapitalReservationService,
    ReservationError,
    ReservationNotFoundError,
)
from trading.storage import ReservationConflictError, TradingEventType, TradingStore

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
MARGIN = f.money("700000")


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def id_factory(clock: FrozenClock) -> SequentialIdFactory:
    return SequentialIdFactory(clock.instant)


@pytest.fixture
def store(clock: FrozenClock, tmp_path: Path) -> TradingStore:
    return TradingStore.open(tmp_path / "trading.db", clock=clock)


@pytest.fixture
def service(
    store: TradingStore,
    clock: FrozenClock,
    id_factory: SequentialIdFactory,
) -> CapitalReservationService:
    return CapitalReservationService(store, clock=clock, id_factory=id_factory)


def _reserve(
    service: CapitalReservationService,
    *,
    intent_id: str,
    amount: str,
    risk_decision_id: str,
) -> CapitalReservation:
    return service.try_reserve(
        intent_id=intent_id,
        strategy_id="positional_index_options_poc",
        amount=f.money(amount),
        margin_available=MARGIN,
        risk_decision_id=risk_decision_id,
    )


class TestTryReserve:
    def test_reserve_succeeds_when_capital_is_available(
        self,
        service: CapitalReservationService,
        store: TradingStore,
    ) -> None:
        """A reservation within budget moves to RESERVED."""
        reservation = _reserve(
            service,
            intent_id="INT-1",
            amount="100000",
            risk_decision_id="DEC-1",
        )
        assert reservation.state is ReservationState.RESERVED
        assert reservation.amount == f.money("100000")
        assert store.get_reservation(reservation.reservation_id) == reservation
        stored = store.read_events()
        assert len(stored) == 1
        assert stored[0].event_type is TradingEventType.CAPITAL_RESERVATION

    def test_reserve_rejects_when_budget_is_exhausted(
        self,
        service: CapitalReservationService,
    ) -> None:
        """Insufficient margin produces a zero-amount REJECTED reservation."""
        reservation = _reserve(
            service,
            intent_id="INT-1",
            amount="800000",
            risk_decision_id="DEC-1",
        )
        assert reservation.state is ReservationState.REJECTED
        assert reservation.amount.is_zero
        assert ReasonCode.CAPITAL_UNAVAILABLE in reservation.reason_codes

    def test_two_reserves_cannot_overspend(
        self,
        service: CapitalReservationService,
        store: TradingStore,
    ) -> None:
        """Invariant 14: concurrent strategies cannot reserve the same capital."""
        first = _reserve(
            service,
            intent_id="INT-1",
            amount="400000",
            risk_decision_id="DEC-1",
        )
        second = _reserve(
            service,
            intent_id="INT-2",
            amount="400000",
            risk_decision_id="DEC-2",
        )
        assert first.state is ReservationState.RESERVED
        assert second.state is ReservationState.REJECTED
        held = sum_active_reservations(
            store.list_reservations(),
            currency=MARGIN.currency,
        )
        assert held == f.money("400000")


class TestReservationLifecycle:
    def test_commit_moves_reserved_to_committed(
        self,
        service: CapitalReservationService,
    ) -> None:
        """Broker confirmation commits the reserved hold."""
        reserved = _reserve(
            service,
            intent_id="INT-1",
            amount="50000",
            risk_decision_id="DEC-1",
        )
        committed = service.commit(reserved.reservation_id)
        assert committed.state is ReservationState.COMMITTED
        assert committed.amount == f.money("50000")

    def test_release_reserved_capital(
        self,
        service: CapitalReservationService,
        store: TradingStore,
    ) -> None:
        """A cancelled order releases its reservation."""
        reserved = _reserve(
            service,
            intent_id="INT-1",
            amount="50000",
            risk_decision_id="DEC-1",
        )
        released = service.release(
            reserved.reservation_id,
            trigger=Trigger.LOCAL_COMMAND,
        )
        assert released.state is ReservationState.RELEASED
        assert released.released_at == NOW
        held = sum_active_reservations(
            store.list_reservations(),
            currency=MARGIN.currency,
        )
        assert held.is_zero

    def test_release_committed_capital_on_reconciliation(
        self,
        service: CapitalReservationService,
    ) -> None:
        """Committed capital releases after reconciliation closes the trade."""
        reserved = _reserve(
            service,
            intent_id="INT-1",
            amount="50000",
            risk_decision_id="DEC-1",
        )
        committed = service.commit(reserved.reservation_id)
        released = service.release(
            committed.reservation_id,
            trigger=Trigger.RECONCILIATION,
        )
        assert released.state is ReservationState.RELEASED

    def test_illegal_transition_is_rejected(
        self,
        service: CapitalReservationService,
    ) -> None:
        """Lifecycle edges are enforced by the reservation state machine."""
        reserved = _reserve(
            service,
            intent_id="INT-1",
            amount="50000",
            risk_decision_id="DEC-1",
        )
        with pytest.raises(ReservationError, match="not permitted on trigger"):
            service.release(reserved.reservation_id, trigger=Trigger.STRATEGY)

    def test_unknown_reservation_raises(
        self,
        service: CapitalReservationService,
    ) -> None:
        with pytest.raises(ReservationNotFoundError, match="RES-MISSING"):
            service.commit("RES-MISSING")

    def test_cas_conflict_raises_on_stale_state(
        self,
        service: CapitalReservationService,
        store: TradingStore,
    ) -> None:
        """A stale expected state fails closed instead of double-transitioning."""
        reserved = _reserve(
            service,
            intent_id="INT-1",
            amount="50000",
            risk_decision_id="DEC-1",
        )
        service.commit(reserved.reservation_id)
        stale = reserved.model_copy(
            update={
                "state": ReservationState.RELEASED,
                "released_at": NOW,
                "updated_at": NOW,
            }
        )
        with pytest.raises(ReservationConflictError):
            store.cas_update_reservation(
                stale,
                expected_state=ReservationState.RESERVED,
                event_id="EVT-STALE",
            )


class TestConcurrentReserve:
    def test_parallel_reserves_cannot_overspend(
        self,
        tmp_path: Path,
    ) -> None:
        """Two threads racing for the same budget leave at most one winner."""
        clock = FrozenClock(NOW)
        store = TradingStore.open(tmp_path / "concurrent.db", clock=clock)
        barrier = threading.Barrier(2)
        results: list[CapitalReservation] = []
        errors: list[BaseException] = []
        plans = (
            ("RES-A", "INT-A", "DEC-A", "EVT-A"),
            ("RES-B", "INT-B", "DEC-B", "EVT-B"),
        )

        def worker(
            reservation_id: str,
            intent_id: str,
            decision_id: str,
            event_id: str,
        ) -> None:
            requested = CapitalReservation(
                reservation_id=reservation_id,
                intent_id=intent_id,
                strategy_id="positional_index_options_poc",
                risk_decision_id=decision_id,
                state=ReservationState.REQUESTED,
                amount=f.money("400000"),
                created_at=NOW,
                updated_at=NOW,
            )
            barrier.wait()
            try:
                results.append(
                    store.atomic_reserve_capital(
                        requested,
                        margin_available=MARGIN,
                        event_id=event_id,
                        recorded_at=NOW,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=plan) for plan in plans
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        assert len(results) == 2
        reserved = [item for item in results if item.state is ReservationState.RESERVED]
        rejected = [item for item in results if item.state is ReservationState.REJECTED]
        assert len(reserved) == 1
        assert len(rejected) == 1
        held = sum_active_reservations(
            store.list_reservations(),
            currency=MARGIN.currency,
        )
        assert held == f.money("400000")
        store.close()
