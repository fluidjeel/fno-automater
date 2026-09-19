"""Durable append-only trading event store backed by SQLite.

Invariant 9: restart recovery replays the event sequence to reconstruct state.
Invariant 11: idempotency keys are unique; duplicates fail closed.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum, unique
from pathlib import Path
from typing import Any

from trading.domain.clock import Clock
from trading.domain.contracts import (
    CapitalReservation,
    EntryFreezeRecord,
    OrderEvent,
    PositionLifecycleRecord,
    ReconciliationEvent,
    RiskDecision,
    VersionedModel,
)
from trading.domain.enums import (
    Exchange,
    ReasonCode,
    ReservationState,
    ReviewSlotId,
    SystemState,
)
from trading.domain.primitives import Currency, Money

__all__ = [
    "AppendSpec",
    "DuplicateIdempotencyKeyError",
    "ReservationConflictError",
    "StoredTradingEvent",
    "TradingEventType",
    "TradingStore",
    "TradingStoreError",
]

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


@unique
class TradingEventType(StrEnum):
    """Discriminator for rows in trading_events."""

    ORDER_EVENT = "order_event"
    RISK_DECISION = "risk_decision"
    RECONCILIATION_EVENT = "reconciliation_event"
    CAPITAL_RESERVATION = "capital_reservation"
    POSITION_LIFECYCLE = "position_lifecycle"
    ENTRY_FREEZE = "entry_freeze"


_PAYLOAD_TYPES: dict[TradingEventType, type[VersionedModel]] = {
    TradingEventType.ORDER_EVENT: OrderEvent,
    TradingEventType.RISK_DECISION: RiskDecision,
    TradingEventType.RECONCILIATION_EVENT: ReconciliationEvent,
    TradingEventType.CAPITAL_RESERVATION: CapitalReservation,
    TradingEventType.POSITION_LIFECYCLE: PositionLifecycleRecord,
    TradingEventType.ENTRY_FREEZE: EntryFreezeRecord,
}


class TradingStoreError(Exception):
    """Base error for durable store failures."""


class DuplicateIdempotencyKeyError(TradingStoreError):
    """Invariant 11: one logical order keeps one idempotency key."""

    def __init__(self, idempotency_key: str, owner_ref: str | None = None) -> None:
        self.idempotency_key = idempotency_key
        self.owner_ref = owner_ref
        message = f"DUPLICATE_IDEMPOTENCY_KEY: {idempotency_key}"
        if owner_ref is not None:
            message = f"{message} (existing owner: {owner_ref})"
        super().__init__(message)


class ReservationConflictError(TradingStoreError):
    """CAS update failed because the stored reservation state changed."""

    def __init__(
        self,
        reservation_id: str,
        *,
        expected: ReservationState | None,
        actual: ReservationState | None,
    ) -> None:
        self.reservation_id = reservation_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"reservation {reservation_id} state conflict: "
            f"expected {expected}, actual {actual}"
        )


@dataclass(frozen=True, slots=True)
class AppendSpec:
    """One append request within a transactional batch."""

    event_type: TradingEventType
    payload: VersionedModel
    event_id: str
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class StoredTradingEvent:
    """One recovered row from trading_events."""

    sequence: int
    event_id: str
    event_type: TradingEventType
    payload: dict[str, Any]
    idempotency_key: str | None
    recorded_at: datetime

    def deserialize(self) -> VersionedModel:
        """Rehydrate the stored JSON payload into its contract type."""
        model_type = _PAYLOAD_TYPES[self.event_type]
        return model_type.model_validate(self.payload)


class TradingStore:
    """Transactional SQLite store for Layer 2 trading events."""

    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._conn = connection
        self._conn.row_factory = sqlite3.Row
        self._clock = clock
        self._lock = threading.Lock()

    @classmethod
    def open(cls, db_path: Path | str, *, clock: Clock) -> TradingStore:
        """Open or create a store at db_path."""
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(path), check_same_thread=False)
        store = cls(connection, clock)
        store._initialize()
        return store

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    def append(
        self,
        event_type: TradingEventType,
        payload: VersionedModel,
        *,
        event_id: str,
        idempotency_key: str | None = None,
        recorded_at: datetime | None = None,
    ) -> int:
        """Append one event and return its monotonic sequence number."""
        spec = AppendSpec(
            event_type=event_type,
            payload=payload,
            event_id=event_id,
            idempotency_key=idempotency_key,
        )
        sequences = self.append_batch((spec,), recorded_at=recorded_at)
        return sequences[0]

    def append_batch(
        self,
        events: Sequence[AppendSpec],
        *,
        recorded_at: datetime | None = None,
    ) -> list[int]:
        """Append events atomically; rollback the whole batch on any failure."""
        if not events:
            return []
        stamp = _utc_iso(recorded_at or self._clock.now_utc())
        with self._transaction():
            sequences: list[int] = []
            for spec in events:
                sequence = self._insert_event(spec, stamp)
                if spec.idempotency_key is not None:
                    self._register_idempotency_key(
                        spec.idempotency_key,
                        spec.event_id,
                        stamp,
                    )
                sequences.append(sequence)
            return sequences

    def register_idempotency_key(
        self,
        idempotency_key: str,
        owner_ref: str,
        *,
        recorded_at: datetime | None = None,
    ) -> None:
        """Record an idempotency key; duplicates raise DuplicateIdempotencyKeyError."""
        stamp = _utc_iso(recorded_at or self._clock.now_utc())
        with self._transaction():
            self._register_idempotency_key(idempotency_key, owner_ref, stamp)

    def read_events(self, *, after_sequence: int = 0) -> tuple[StoredTradingEvent, ...]:
        """Return events in ascending sequence order for crash recovery."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT sequence, event_id, event_type, payload, idempotency_key, "
                "recorded_at "
                "FROM trading_events "
                "WHERE sequence > ? "
                "ORDER BY sequence ASC",
                (after_sequence,),
            ).fetchall()
        return tuple(_row_to_stored_event(row) for row in rows)

    def upsert_reservation(
        self,
        reservation: CapitalReservation,
        *,
        updated_at: datetime | None = None,
    ) -> None:
        """Persist the latest reservation state for atomic CAS updates."""
        stamp = _utc_iso(updated_at or reservation.updated_at)
        with self._transaction():
            self._upsert_reservation_row(reservation, stamp)

    def atomic_reserve_capital(
        self,
        reservation: CapitalReservation,
        *,
        margin_available: Money,
        event_id: str,
        recorded_at: datetime | None = None,
    ) -> CapitalReservation:
        """Reserve capital atomically; reject when existing holds exceed budget."""
        if reservation.state is not ReservationState.REQUESTED:
            raise TradingStoreError(
                "atomic_reserve_capital requires a REQUESTED reservation"
            )
        stamp = _utc_iso(recorded_at or reservation.updated_at)
        with self._transaction():
            existing = self._get_reservation_row(reservation.reservation_id)
            if existing is not None:
                payload = json.loads(existing["payload"])
                return CapitalReservation.model_validate(payload)
            held = self._sum_active_reservation_amount(margin_available.currency)
            affordable = held + reservation.amount <= margin_available
            if affordable:
                final = reservation.model_copy(
                    update={
                        "state": ReservationState.RESERVED,
                        "updated_at": reservation.updated_at,
                    }
                )
            else:
                final = reservation.model_copy(
                    update={
                        "state": ReservationState.REJECTED,
                        "amount": Money.zero(margin_available.currency),
                        "reason_codes": (ReasonCode.CAPITAL_UNAVAILABLE,),
                        "updated_at": reservation.updated_at,
                    }
                )
            self._upsert_reservation_row(final, stamp)
            self._insert_event(
                AppendSpec(
                    event_type=TradingEventType.CAPITAL_RESERVATION,
                    payload=final,
                    event_id=event_id,
                ),
                stamp,
            )
            return final

    def cas_update_reservation(
        self,
        reservation: CapitalReservation,
        *,
        expected_state: ReservationState,
        event_id: str,
        recorded_at: datetime | None = None,
    ) -> CapitalReservation:
        """Update a reservation only when its stored state matches expected_state."""
        stamp = _utc_iso(recorded_at or reservation.updated_at)
        with self._transaction():
            row = self._get_reservation_row(reservation.reservation_id)
            if row is None:
                raise ReservationConflictError(
                    reservation.reservation_id,
                    expected=expected_state,
                    actual=None,
                )
            actual = ReservationState(str(row["state"]))
            if actual is not expected_state:
                raise ReservationConflictError(
                    reservation.reservation_id,
                    expected=expected_state,
                    actual=actual,
                )
            self._upsert_reservation_row(reservation, stamp)
            self._insert_event(
                AppendSpec(
                    event_type=TradingEventType.CAPITAL_RESERVATION,
                    payload=reservation,
                    event_id=event_id,
                ),
                stamp,
            )
            return reservation

    def get_reservation(self, reservation_id: str) -> CapitalReservation | None:
        """Load the current reservation snapshot, if present."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
        if row is None:
            return None
        return CapitalReservation.model_validate(json.loads(row["payload"]))

    def list_reservations(self) -> tuple[CapitalReservation, ...]:
        """Return every reservation row for portfolio reconstruction."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM reservations ORDER BY reservation_id ASC"
            ).fetchall()
        return tuple(
            CapitalReservation.model_validate(json.loads(row["payload"]))
            for row in rows
        )

    def set_system_state(
        self,
        state: SystemState,
        *,
        last_reconciliation_ref: str | None = None,
        updated_at: datetime | None = None,
    ) -> None:
        """Persist the singleton system readiness row."""
        stamp = _utc_iso(updated_at or self._clock.now_utc())
        with self._transaction():
            self._conn.execute(
                "INSERT INTO system_state "
                "(singleton, state, last_reconciliation_ref, updated_at) "
                "VALUES (1, ?, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "state = excluded.state, "
                "last_reconciliation_ref = excluded.last_reconciliation_ref, "
                "updated_at = excluded.updated_at",
                (state.value, last_reconciliation_ref, stamp),
            )

    def get_system_state(self) -> tuple[SystemState, str | None]:
        """Return the current system state and last reconciliation reference."""
        with self._lock:
            row = self._conn.execute(
                "SELECT state, last_reconciliation_ref "
                "FROM system_state WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return SystemState.STARTING, None
        ref = row["last_reconciliation_ref"]
        return SystemState(row["state"]), ref if isinstance(ref, str) else None

    def upsert_position_lifecycle(
        self,
        record: PositionLifecycleRecord,
        *,
        event_id: str,
        recorded_at: datetime | None = None,
    ) -> None:
        """Replace the current lifecycle snapshot and append an audit event."""
        stamp = _utc_iso(recorded_at or record.as_of)
        with self._transaction():
            self._conn.execute(
                "INSERT INTO position_lifecycle "
                "(trade_id, state, payload, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(trade_id) DO UPDATE SET "
                "state = excluded.state, "
                "payload = excluded.payload, "
                "updated_at = excluded.updated_at",
                (
                    record.trade_id,
                    record.position.state.value,
                    record.model_dump_json(),
                    stamp,
                ),
            )
            self._insert_event(
                AppendSpec(
                    event_type=TradingEventType.POSITION_LIFECYCLE,
                    payload=record,
                    event_id=event_id,
                ),
                stamp,
            )

    def get_position_lifecycle(self, trade_id: str) -> PositionLifecycleRecord | None:
        """Load the latest persisted lifecycle for one trade."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM position_lifecycle WHERE trade_id = ?",
                (trade_id,),
            ).fetchone()
        if row is None:
            return None
        return PositionLifecycleRecord.model_validate(json.loads(row["payload"]))

    def list_position_lifecycle(self) -> tuple[PositionLifecycleRecord, ...]:
        """Return every persisted position lifecycle snapshot."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM position_lifecycle ORDER BY trade_id ASC"
            ).fetchall()
        return tuple(
            PositionLifecycleRecord.model_validate(json.loads(row["payload"]))
            for row in rows
        )

    def record_review_slot_run(
        self,
        *,
        slot_id: ReviewSlotId,
        session_date: date,
        venue: Exchange,
        as_of: datetime,
    ) -> bool:
        """Persist that a review slot ran. Returns False on a duplicate day+slot."""
        stamp = _utc_iso(as_of)
        with self._transaction():
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO review_slot_runs "
                "(slot_id, session_date, venue, as_of) VALUES (?, ?, ?, ?)",
                (slot_id.value, session_date.isoformat(), venue.value, stamp),
            )
            return cursor.rowcount == 1

    def has_review_slot_run(self, slot_id: ReviewSlotId, session_date: date) -> bool:
        """Whether this NSE/MCX slot already ran on the IST session date."""
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM review_slot_runs WHERE slot_id = ? AND session_date = ?",
                (slot_id.value, session_date.isoformat()),
            ).fetchone()
        return row is not None

    def list_review_slot_runs(
        self, session_date: date | None = None
    ) -> tuple[tuple[ReviewSlotId, date], ...]:
        """Return recorded (slot, session_date) pairs, optionally one day."""
        with self._lock:
            if session_date is None:
                rows = self._conn.execute(
                    "SELECT slot_id, session_date FROM review_slot_runs "
                    "ORDER BY session_date ASC, slot_id ASC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT slot_id, session_date FROM review_slot_runs "
                    "WHERE session_date = ? ORDER BY slot_id ASC",
                    (session_date.isoformat(),),
                ).fetchall()
        return tuple(
            (
                ReviewSlotId(str(row["slot_id"])),
                date.fromisoformat(str(row["session_date"])),
            )
            for row in rows
        )

    def get_entry_freeze(self) -> EntryFreezeRecord | None:
        """Load the persisted entry-freeze latch, if any."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM entry_freeze WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        return EntryFreezeRecord.model_validate(json.loads(row["payload"]))

    def upsert_entry_freeze(
        self,
        record: EntryFreezeRecord,
        *,
        event_id: str,
        recorded_at: datetime | None = None,
    ) -> bool:
        """Persist the freeze latch. Returns True when an audit event was written.

        Unchanged blocked/reason/detail combinations skip the audit append so
        restart recovery and duplicate freeze calls stay idempotent.
        """
        existing = self.get_entry_freeze()
        unchanged = (
            existing is not None
            and existing.entries_blocked == record.entries_blocked
            and existing.reason_code == record.reason_code
            and existing.detail == record.detail
        )
        stamp = _utc_iso(recorded_at or record.updated_at)
        with self._transaction():
            self._conn.execute(
                "INSERT INTO entry_freeze "
                "(singleton, entries_blocked, reason_code, detail, payload, "
                "updated_at) "
                "VALUES (1, ?, ?, ?, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "entries_blocked = excluded.entries_blocked, "
                "reason_code = excluded.reason_code, "
                "detail = excluded.detail, "
                "payload = excluded.payload, "
                "updated_at = excluded.updated_at",
                (
                    1 if record.entries_blocked else 0,
                    None if record.reason_code is None else record.reason_code.value,
                    record.detail,
                    record.model_dump_json(),
                    stamp,
                ),
            )
            if unchanged:
                return False
            self._insert_event(
                AppendSpec(
                    event_type=TradingEventType.ENTRY_FREEZE,
                    payload=record,
                    event_id=event_id,
                ),
                stamp,
            )
            return True

    def _initialize(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _get_reservation_row(self, reservation_id: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._conn.execute(
            "SELECT state, payload FROM reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        return row

    def _sum_active_reservation_amount(self, currency: Currency) -> Money:
        total = Money.zero(currency)
        rows = self._conn.execute("SELECT payload FROM reservations").fetchall()
        for row in rows:
            reservation = CapitalReservation.model_validate(json.loads(row["payload"]))
            if reservation.state.holds_capital:
                total = total + reservation.amount
        return total

    def _upsert_reservation_row(
        self,
        reservation: CapitalReservation,
        updated_at: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO reservations (reservation_id, state, payload, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(reservation_id) DO UPDATE SET "
            "state = excluded.state, "
            "payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (
                reservation.reservation_id,
                reservation.state.value,
                reservation.model_dump_json(),
                updated_at,
            ),
        )

    def _insert_event(self, spec: AppendSpec, recorded_at: str) -> int:
        cursor = self._conn.execute(
            "INSERT INTO trading_events "
            "(event_id, event_type, payload, idempotency_key, recorded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                spec.event_id,
                spec.event_type.value,
                spec.payload.model_dump_json(),
                spec.idempotency_key,
                recorded_at,
            ),
        )
        sequence = cursor.lastrowid
        if sequence is None:
            raise TradingStoreError("append did not return a sequence number")
        return int(sequence)

    def _register_idempotency_key(
        self,
        idempotency_key: str,
        owner_ref: str,
        registered_at: str,
    ) -> None:
        try:
            self._conn.execute(
                "INSERT INTO idempotency_keys "
                "(idempotency_key, owner_ref, registered_at) "
                "VALUES (?, ?, ?)",
                (idempotency_key, owner_ref, registered_at),
            )
        except sqlite3.IntegrityError:
            existing = self._conn.execute(
                "SELECT owner_ref FROM idempotency_keys WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            owner = existing["owner_ref"] if existing is not None else None
            raise DuplicateIdempotencyKeyError(idempotency_key, owner) from None


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _row_to_stored_event(row: sqlite3.Row) -> StoredTradingEvent:
    payload = json.loads(row["payload"])
    if not isinstance(payload, dict):
        raise TradingStoreError(f"event {row['event_id']} payload is not a JSON object")
    key = row["idempotency_key"]
    return StoredTradingEvent(
        sequence=int(row["sequence"]),
        event_id=str(row["event_id"]),
        event_type=TradingEventType(str(row["event_type"])),
        payload=payload,
        idempotency_key=key if isinstance(key, str) else None,
        recorded_at=datetime.fromisoformat(str(row["recorded_at"])),
    )
