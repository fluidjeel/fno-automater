"""Durable trading event store (L2-002).

Invariant 9: recovery replays the event sequence after restart.
Invariant 11: duplicate idempotency keys are rejected.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

import tests.factories as f
from trading.domain.clock import FrozenClock
from trading.domain.contracts import (
    CapitalReservation,
    OrderEvent,
    ReconciliationEvent,
    RiskDecision,
)
from trading.domain.enums import Exchange, ReservationState, ReviewSlotId, SystemState
from trading.storage.trading_store import (
    AppendSpec,
    DuplicateIdempotencyKeyError,
    TradingEventType,
    TradingStore,
)

NOW = datetime(2026, 9, 14, 4, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=1)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def store(tmp_path: Path, clock: FrozenClock) -> Iterator[TradingStore]:
    trading_store = TradingStore.open(tmp_path / "trading.sqlite", clock=clock)
    yield trading_store
    trading_store.close()


class TestTransactionalAppend:
    def test_append_returns_monotonic_sequence(self, store: TradingStore) -> None:
        """Each append receives the next sequence number."""
        first = store.append(
            TradingEventType.RISK_DECISION,
            f.risk_decision(decision_id="DEC-1"),
            event_id="EVT-1",
        )
        second = store.append(
            TradingEventType.RISK_DECISION,
            f.risk_decision(decision_id="DEC-2"),
            event_id="EVT-2",
        )
        assert first == 1
        assert second == 2

    def test_duplicate_event_id_fails(self, store: TradingStore) -> None:
        """Event IDs are unique within trading_events."""
        store.append(
            TradingEventType.ORDER_EVENT,
            f.order_event(event_id="EVT-DUP"),
            event_id="EVT-DUP",
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.append(
                TradingEventType.ORDER_EVENT,
                f.order_event(event_id="EVT-DUP-2"),
                event_id="EVT-DUP",
            )

    def test_batch_append_is_atomic(self, store: TradingStore) -> None:
        """A failed batch rolls back every event in the batch."""
        key = "83e65c7aafd8d0272bdd320d1d437938"
        store.register_idempotency_key(key, "ORD-EXISTING")
        specs = (
            AppendSpec(
                event_type=TradingEventType.ORDER_EVENT,
                payload=f.order_event(event_id="EVT-OK"),
                event_id="EVT-OK",
            ),
            AppendSpec(
                event_type=TradingEventType.ORDER_EVENT,
                payload=f.order_event(event_id="EVT-BAD"),
                event_id="EVT-BAD",
                idempotency_key=key,
            ),
        )
        with pytest.raises(DuplicateIdempotencyKeyError):
            store.append_batch(specs)
        assert store.read_events() == ()

    def test_batch_append_commits_all_events(self, store: TradingStore) -> None:
        """Successful batches persist every event in order."""
        specs = (
            AppendSpec(
                event_type=TradingEventType.RISK_DECISION,
                payload=f.risk_decision(decision_id="DEC-A"),
                event_id="EVT-A",
            ),
            AppendSpec(
                event_type=TradingEventType.RECONCILIATION_EVENT,
                payload=f.reconciliation_event(event_id="REC-A"),
                event_id="REC-A",
            ),
        )
        sequences = store.append_batch(specs)
        recovered = store.read_events()
        assert sequences == [1, 2]
        assert [event.sequence for event in recovered] == [1, 2]
        assert [event.event_id for event in recovered] == ["EVT-A", "REC-A"]


class TestIdempotencyKeys:
    def test_duplicate_idempotency_key_raises(self, store: TradingStore) -> None:
        """Invariant 11: one logical order keeps one idempotency key."""
        key = "83e65c7aafd8d0272bdd320d1d437938"
        store.register_idempotency_key(key, "ORD-1")
        with pytest.raises(DuplicateIdempotencyKeyError) as exc_info:
            store.register_idempotency_key(key, "ORD-2")
        error = exc_info.value
        assert error.idempotency_key == key
        assert error.owner_ref == "ORD-1"
        assert "DUPLICATE_IDEMPOTENCY_KEY" in str(error)

    def test_append_registers_idempotency_key_in_same_transaction(
        self,
        store: TradingStore,
    ) -> None:
        """Order append and idempotency registration commit together."""
        key = "83e65c7aafd8d0272bdd320d1d437938"
        store.append(
            TradingEventType.ORDER_EVENT,
            f.order_event(event_id="EVT-ORD-1"),
            event_id="EVT-ORD-1",
            idempotency_key=key,
        )
        with pytest.raises(DuplicateIdempotencyKeyError):
            store.register_idempotency_key(key, "ORD-RETRY")


class TestRecovery:
    def test_recovery_reads_event_sequence(
        self,
        tmp_path: Path,
        clock: FrozenClock,
    ) -> None:
        """Invariant 9: restart recovery replays events in sequence order."""
        db_path = tmp_path / "recovery.sqlite"
        writer = TradingStore.open(db_path, clock=clock)
        writer.append(
            TradingEventType.RISK_DECISION,
            f.risk_decision(decision_id="DEC-1"),
            event_id="EVT-1",
            recorded_at=NOW,
        )
        writer.append(
            TradingEventType.ORDER_EVENT,
            f.order_event(event_id="EVT-2"),
            event_id="EVT-2",
            idempotency_key="key-evt-2",
            recorded_at=LATER,
        )
        writer.append(
            TradingEventType.CAPITAL_RESERVATION,
            f.capital_reservation(reservation_id="RES-1"),
            event_id="EVT-3",
            recorded_at=LATER + timedelta(seconds=1),
        )
        writer.close()

        reader = TradingStore.open(db_path, clock=clock)
        recovered = reader.read_events()
        reader.close()

        assert [event.sequence for event in recovered] == [1, 2, 3]
        assert recovered[0].deserialize() == f.risk_decision(decision_id="DEC-1")
        assert isinstance(recovered[1].deserialize(), OrderEvent)
        assert isinstance(recovered[2].deserialize(), CapitalReservation)

    def test_read_events_supports_incremental_replay(self, store: TradingStore) -> None:
        """Consumers can resume from the last processed sequence."""
        store.append(
            TradingEventType.RECONCILIATION_EVENT,
            f.reconciliation_event(event_id="REC-1"),
            event_id="REC-1",
        )
        store.append(
            TradingEventType.RECONCILIATION_EVENT,
            f.reconciliation_event(event_id="REC-2"),
            event_id="REC-2",
        )
        tail = store.read_events(after_sequence=1)
        assert len(tail) == 1
        assert tail[0].event_id == "REC-2"
        assert isinstance(tail[0].deserialize(), ReconciliationEvent)


class TestSupportingTables:
    def test_reservation_upsert_round_trip(self, store: TradingStore) -> None:
        """Reservation CAS table stores the latest reservation snapshot."""
        reserved = f.capital_reservation(
            reservation_id="RES-1",
            state=ReservationState.RESERVED,
        )
        store.upsert_reservation(reserved)
        assert store.get_reservation("RES-1") == reserved

        released = f.capital_reservation(
            reservation_id="RES-1",
            state=ReservationState.RELEASED,
            released_at=LATER,
            updated_at=LATER,
        )
        store.upsert_reservation(released)
        assert store.get_reservation("RES-1") == released

    def test_position_lifecycle_upsert_is_idempotent_by_trade_id(
        self, store: TradingStore
    ) -> None:
        """Invariant 9: restart reads the latest lifecycle snapshot, not duplicates."""
        first = f.position_lifecycle_record()
        store.upsert_position_lifecycle(first, event_id="PLC-1")
        tightened = first.model_copy(
            update={
                "position": first.position.model_copy(update={"as_of": LATER}),
                "as_of": LATER,
            }
        )
        store.upsert_position_lifecycle(tightened, event_id="PLC-2")
        loaded = store.get_position_lifecycle(first.trade_id)
        assert loaded is not None
        assert loaded.as_of == LATER
        listed = store.list_position_lifecycle()
        assert len(listed) == 1
        events = [
            event
            for event in store.read_events()
            if event.event_type is TradingEventType.POSITION_LIFECYCLE
        ]
        assert len(events) == 2

    def test_system_state_round_trip(self, store: TradingStore) -> None:
        """System readiness persists across sessions."""
        assert store.get_system_state() == (SystemState.STARTING, None)
        store.set_system_state(
            SystemState.RECOVERY,
            last_reconciliation_ref="REC-BOOT-1",
            updated_at=NOW,
        )
        assert store.get_system_state() == (SystemState.RECOVERY, "REC-BOOT-1")

    def test_review_slot_run_is_idempotent(self, store: TradingStore) -> None:
        """Duplicate invocation of the same slot+day inserts once."""
        first = store.record_review_slot_run(
            slot_id=ReviewSlotId.NSE_MORNING,
            session_date=date(2026, 9, 14),
            venue=Exchange.NSE,
            as_of=NOW,
        )
        second = store.record_review_slot_run(
            slot_id=ReviewSlotId.NSE_MORNING,
            session_date=date(2026, 9, 14),
            venue=Exchange.NSE,
            as_of=LATER,
        )
        assert first is True
        assert second is False
        assert store.has_review_slot_run(ReviewSlotId.NSE_MORNING, date(2026, 9, 14))
        assert not store.has_review_slot_run(
            ReviewSlotId.NSE_AFTERNOON, date(2026, 9, 14)
        )

    def test_deserialize_preserves_contract_fields(self, store: TradingStore) -> None:
        """Recovered payloads round-trip through their contract types."""
        decision = f.risk_decision(decision_id="DEC-RT")
        store.append(
            TradingEventType.RISK_DECISION,
            decision,
            event_id="EVT-RT",
        )
        stored = store.read_events()[0]
        recovered = stored.deserialize()
        assert isinstance(recovered, RiskDecision)
        assert recovered == decision
