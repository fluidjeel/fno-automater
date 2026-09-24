"""Atomic capital reservation. Invariant 14."""

from __future__ import annotations

from trading.domain.clock import Clock
from trading.domain.contracts.reservation import CapitalReservation
from trading.domain.enums import ModeId, ReservationState, Trigger
from trading.domain.ids import IdFactory
from trading.domain.primitives import Money
from trading.domain.state import RESERVATION_MACHINE, IllegalTransitionError
from trading.storage.trading_store import TradingStore

__all__ = [
    "CapitalReservationService",
    "ReservationError",
    "ReservationNotFoundError",
]


class ReservationError(Exception):
    """Base error for reservation lifecycle failures."""


class ReservationNotFoundError(ReservationError):
    """Raised when a reservation id is unknown to the store."""

    def __init__(self, reservation_id: str) -> None:
        self.reservation_id = reservation_id
        super().__init__(f"unknown reservation: {reservation_id}")


class CapitalReservationService:
    """Reserve, commit and release capital against a durable store."""

    def __init__(
        self,
        store: TradingStore,
        *,
        clock: Clock,
        id_factory: IdFactory,
    ) -> None:
        self._store = store
        self._clock = clock
        self._ids = id_factory

    def try_reserve(
        self,
        *,
        intent_id: str,
        strategy_id: str,
        amount: Money,
        margin_available: Money,
        risk_decision_id: str,
        mode_id: ModeId | None = None,
        idempotency_key: str | None = None,
    ) -> CapitalReservation:
        """Atomically reserve capital or reject when the budget is exhausted."""
        now = self._clock.now_utc()
        requested = CapitalReservation(
            reservation_id=self._ids.new_id("RES"),
            intent_id=intent_id,
            strategy_id=strategy_id,
            risk_decision_id=risk_decision_id,
            mode_id=mode_id,
            idempotency_key=idempotency_key,
            state=ReservationState.REQUESTED,
            amount=amount,
            created_at=now,
            updated_at=now,
        )
        return self._store.atomic_reserve_capital(
            requested,
            margin_available=margin_available,
            event_id=self._ids.new_id("EVT"),
            recorded_at=now,
        )

    def commit(self, reservation_id: str) -> CapitalReservation:
        """Move a reserved hold to committed after broker confirmation."""
        return self._transition(
            reservation_id,
            ReservationState.COMMITTED,
            trigger=Trigger.BROKER_EVENT,
        )

    def release(self, reservation_id: str, *, trigger: Trigger) -> CapitalReservation:
        """Release a reserved or committed hold back to available capital."""
        return self._transition(
            reservation_id,
            ReservationState.RELEASED,
            trigger=trigger,
        )

    def _transition(
        self,
        reservation_id: str,
        target: ReservationState,
        *,
        trigger: Trigger,
    ) -> CapitalReservation:
        current = self._store.get_reservation(reservation_id)
        if current is None:
            raise ReservationNotFoundError(reservation_id)
        now = self._clock.now_utc()
        try:
            RESERVATION_MACHINE.transition(
                current.state,
                target,
                trigger=trigger,
                at=now,
            )
        except IllegalTransitionError as exc:
            raise ReservationError(exc.record.detail) from exc

        updates: dict[str, object] = {
            "state": target,
            "updated_at": now,
        }
        if target is ReservationState.RELEASED:
            updates["released_at"] = now
        updated = current.model_copy(update=updates)
        return self._store.cas_update_reservation(
            updated,
            expected_state=current.state,
            event_id=self._ids.new_id("EVT"),
            recorded_at=now,
        )
