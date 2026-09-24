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
from decimal import Decimal
from enum import StrEnum, unique
from pathlib import Path
from typing import Any

from trading.analytics.improvements import merge_duplicate, normalize_claim_key
from trading.domain.clock import Clock
from trading.domain.contracts import (
    AgentDecision,
    AuthorityGrant,
    CapitalReservation,
    EntryFreezeRecord,
    HallucinationEvent,
    ImprovementRecord,
    OrderEvent,
    PaperCycleEvidence,
    PositionLifecycleRecord,
    ProtectionStateRecord,
    ReconciliationEvent,
    RiskDecision,
    SessionProtectionState,
    VersionedModel,
)
from trading.domain.contracts.agent_budget import AgentBudgetSnapshot
from trading.domain.contracts.campaign import CampaignRecord
from trading.domain.contracts.fill_charges import FillChargeRecord
from trading.domain.enums import (
    DeskRole,
    Exchange,
    ImprovementArea,
    ImprovementStatus,
    ModeId,
    ReasonCode,
    ReservationState,
    ReviewSlotId,
    SystemState,
)
from trading.domain.primitives import Currency, Money
from trading.storage.schema_migration import (
    apply_data_migrations,
    apply_schema_migrations,
    database_has_trading_events,
)

__all__ = [
    "AppendSpec",
    "DuplicateAgentDecisionError",
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
    CAMPAIGN_LEDGER = "campaign_ledger"
    FILL_CHARGE = "fill_charge"
    ENTRY_FREEZE = "entry_freeze"
    CYCLE_EVIDENCE = "cycle_evidence"


_PAYLOAD_TYPES: dict[TradingEventType, type[VersionedModel]] = {
    TradingEventType.ORDER_EVENT: OrderEvent,
    TradingEventType.RISK_DECISION: RiskDecision,
    TradingEventType.RECONCILIATION_EVENT: ReconciliationEvent,
    TradingEventType.CAPITAL_RESERVATION: CapitalReservation,
    TradingEventType.POSITION_LIFECYCLE: PositionLifecycleRecord,
    TradingEventType.CAMPAIGN_LEDGER: CampaignRecord,
    TradingEventType.FILL_CHARGE: FillChargeRecord,
    TradingEventType.ENTRY_FREEZE: EntryFreezeRecord,
    TradingEventType.CYCLE_EVIDENCE: PaperCycleEvidence,
}


class TradingStoreError(Exception):
    """Base error for durable store failures."""


class DuplicateAgentDecisionError(TradingStoreError):
    """Append-only: decision_id is unique; a retry must not rewrite history."""

    def __init__(self, decision_id: str) -> None:
        self.decision_id = decision_id
        super().__init__(f"DUPLICATE_AGENT_DECISION: {decision_id}")


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
        apply_data_migrations(store)
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
            if reservation.idempotency_key is not None:
                idem_row = self._conn.execute(
                    "SELECT payload FROM reservations WHERE idempotency_key = ?",
                    (reservation.idempotency_key,),
                ).fetchone()
                if idem_row is not None:
                    existing = CapitalReservation.model_validate(
                        json.loads(idem_row["payload"])
                    )
                    if existing.state.holds_capital:
                        return existing
                    reservation = reservation.model_copy(
                        update={"reservation_id": existing.reservation_id}
                    )
            existing = self._get_reservation_row(reservation.reservation_id)
            if existing is not None:
                current = CapitalReservation.model_validate(json.loads(existing["payload"]))
                if current.state.holds_capital:
                    return current
            held = self._sum_active_reservation_amount(
                margin_available.currency,
                mode_id=reservation.mode_id.value
                if reservation.mode_id is not None
                else None,
            )
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
                "(trade_id, state, mode_id, campaign_id, policy_version, payload, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(trade_id) DO UPDATE SET "
                "state = excluded.state, "
                "mode_id = excluded.mode_id, "
                "campaign_id = excluded.campaign_id, "
                "policy_version = excluded.policy_version, "
                "payload = excluded.payload, "
                "updated_at = excluded.updated_at",
                (
                    record.trade_id,
                    record.position.state.value,
                    record.mode_id.value if record.mode_id is not None else None,
                    record.campaign_id,
                    record.policy_version,
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

    def upsert_campaign_record(
        self,
        record: CampaignRecord,
        *,
        event_id: str,
        recorded_at: datetime | None = None,
    ) -> None:
        """Persist one campaign ledger snapshot."""
        stamp = _utc_iso(recorded_at or self._clock.now_utc())
        with self._transaction():
            self._conn.execute(
                "INSERT INTO campaign_ledger "
                "(campaign_id, mode_id, payload, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(campaign_id) DO UPDATE SET "
                "mode_id = excluded.mode_id, "
                "payload = excluded.payload, "
                "updated_at = excluded.updated_at",
                (
                    record.campaign_id,
                    record.mode_id.value,
                    record.model_dump_json(),
                    stamp,
                ),
            )
            self._insert_event(
                AppendSpec(
                    event_type=TradingEventType.CAMPAIGN_LEDGER,
                    payload=record,
                    event_id=event_id,
                ),
                stamp,
            )

    def get_campaign_record(self, campaign_id: str) -> CampaignRecord | None:
        """Load one campaign ledger snapshot."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM campaign_ledger WHERE campaign_id = ?",
                (campaign_id,),
            ).fetchone()
        if row is None:
            return None
        return CampaignRecord.model_validate(json.loads(row["payload"]))

    def list_campaign_records(self) -> tuple[CampaignRecord, ...]:
        """Return every persisted campaign ledger snapshot."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM campaign_ledger ORDER BY campaign_id ASC"
            ).fetchall()
        return tuple(
            CampaignRecord.model_validate(json.loads(row["payload"])) for row in rows
        )

    def upsert_fill_charge(
        self,
        record: FillChargeRecord,
        *,
        event_id: str,
        recorded_at: datetime | None = None,
    ) -> FillChargeRecord:
        """Persist one fill charge row; duplicate fill identity is idempotent."""
        stamp = _utc_iso(recorded_at or record.recorded_at)
        with self._lock:
            existing = self._conn.execute(
                "SELECT payload FROM fill_charges WHERE fill_idempotency_key = ?",
                (record.fill_idempotency_key,),
            ).fetchone()
        if existing is not None:
            prior = FillChargeRecord.model_validate(json.loads(existing["payload"]))
            if (
                prior.inputs.filled_quantity >= record.inputs.filled_quantity
                and prior.order_event_id == record.order_event_id
            ):
                return prior
        append_event_id = (
            f"{event_id}::{record.inputs.filled_quantity}::{record.policy_version}"
        )
        with self._transaction():
            self._conn.execute(
                "INSERT INTO fill_charges "
                "(fill_idempotency_key, trade_id, payload, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(fill_idempotency_key) DO UPDATE SET "
                "trade_id = excluded.trade_id, "
                "payload = excluded.payload, "
                "updated_at = excluded.updated_at",
                (
                    record.fill_idempotency_key,
                    record.trade_id,
                    record.model_dump_json(),
                    stamp,
                ),
            )
            self._insert_event(
                AppendSpec(
                    event_type=TradingEventType.FILL_CHARGE,
                    payload=record,
                    event_id=append_event_id,
                ),
                stamp,
            )
        return record

    def get_fill_charge(self, fill_idempotency_key: str) -> FillChargeRecord | None:
        """Load one fill charge by the same key used for cash-flow dedupe."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM fill_charges WHERE fill_idempotency_key = ?",
                (fill_idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        return FillChargeRecord.model_validate(json.loads(row["payload"]))

    def list_fill_charges(self) -> tuple[FillChargeRecord, ...]:
        """Return every persisted fill charge snapshot."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM fill_charges ORDER BY fill_idempotency_key ASC"
            ).fetchall()
        return tuple(
            FillChargeRecord.model_validate(json.loads(row["payload"])) for row in rows
        )

    def upsert_protection_state(self, record: ProtectionStateRecord) -> None:
        """Persist per-trade protection monitor state."""
        stamp = _utc_iso(record.as_of)
        with self._transaction():
            self._conn.execute(
                "INSERT INTO protection_state "
                "(trade_id, status, payload, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(trade_id) DO UPDATE SET "
                "status = excluded.status, "
                "payload = excluded.payload, "
                "updated_at = excluded.updated_at",
                (
                    record.trade_id,
                    record.status.value,
                    record.model_dump_json(),
                    stamp,
                ),
            )

    def get_protection_state(self, trade_id: str) -> ProtectionStateRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM protection_state WHERE trade_id = ?",
                (trade_id,),
            ).fetchone()
        if row is None:
            return None
        return ProtectionStateRecord.model_validate(json.loads(row["payload"]))

    def list_protection_states(self) -> tuple[ProtectionStateRecord, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM protection_state ORDER BY trade_id ASC"
            ).fetchall()
        return tuple(
            ProtectionStateRecord.model_validate(json.loads(row["payload"]))
            for row in rows
        )

    def upsert_session_protection(self, state: SessionProtectionState) -> None:
        stamp = _utc_iso(state.as_of)
        with self._transaction():
            self._conn.execute(
                "INSERT INTO session_protection (singleton, payload, updated_at) "
                "VALUES (1, ?, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "payload = excluded.payload, "
                "updated_at = excluded.updated_at",
                (state.model_dump_json(), stamp),
            )

    def get_session_protection(self) -> SessionProtectionState | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM session_protection WHERE singleton = 1"
            ).fetchone()
        if row is None:
            return None
        return SessionProtectionState.model_validate(json.loads(row["payload"]))

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

    def get_agent_budget(self, year_month: str, role: str) -> AgentBudgetSnapshot:
        """Return persisted monthly spend for a role, or zero when absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT spent_inr, input_tokens, output_tokens, updated_at "
                "FROM agent_budget_ledger WHERE year_month = ? AND role = ?",
                (year_month, role),
            ).fetchone()
        if row is None:
            return AgentBudgetSnapshot(
                year_month=year_month,
                role=role,
                spent_inr=Decimal("0"),
                input_tokens=0,
                output_tokens=0,
                updated_at=self._clock.now_utc(),
            )
        return AgentBudgetSnapshot(
            year_month=year_month,
            role=role,
            spent_inr=Decimal(str(row["spent_inr"])),
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def upsert_agent_budget(
        self,
        year_month: str,
        role: str,
        *,
        spent_inr: Decimal,
        input_tokens: int,
        output_tokens: int,
        updated_at: datetime | None = None,
    ) -> None:
        """Persist absolute monthly spend for one agent role."""
        stamp = _utc_iso(updated_at or self._clock.now_utc())
        with self._transaction():
            self._conn.execute(
                "INSERT INTO agent_budget_ledger (year_month, role, spent_inr, "
                "input_tokens, output_tokens, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(year_month, role) DO UPDATE SET "
                "spent_inr = excluded.spent_inr, "
                "input_tokens = excluded.input_tokens, "
                "output_tokens = excluded.output_tokens, "
                "updated_at = excluded.updated_at",
                (
                    year_month,
                    role,
                    str(spent_inr),
                    input_tokens,
                    output_tokens,
                    stamp,
                ),
            )

    def insert_authority_grant(self, grant: AuthorityGrant) -> None:
        """Persist an operator-signed grant. Re-validates C1 at write time.

        Agents must never call this. There is no update or renew path.
        """
        verified = AuthorityGrant.model_validate(grant.model_dump(mode="json"))
        stamp_granted = _utc_iso(verified.granted_at)
        stamp_until = _utc_iso(verified.valid_until)
        with self._transaction():
            self._conn.execute(
                "INSERT INTO authority_grants ("
                "grant_id, role, mode, model_id, prompt_version, policy_version, "
                "environment, granted_at, valid_until, signed_by, checksum, payload"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    verified.grant_id,
                    verified.role.value,
                    verified.mode.value,
                    verified.model_id,
                    verified.prompt_version,
                    verified.policy_version,
                    verified.environment.value,
                    stamp_granted,
                    stamp_until,
                    verified.signed_by,
                    verified.checksum,
                    verified.model_dump_json(),
                ),
            )

    def get_authority_grant(self, grant_id: str) -> AuthorityGrant | None:
        """Load one grant by id, or None when absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM authority_grants WHERE grant_id = ?",
                (grant_id,),
            ).fetchone()
        if row is None:
            return None
        return AuthorityGrant.model_validate(json.loads(row["payload"]))

    def list_authority_grants(
        self, *, role: DeskRole | None = None
    ) -> tuple[AuthorityGrant, ...]:
        """Return grants, newest first, optionally filtered by role."""
        with self._lock:
            if role is None:
                rows = self._conn.execute(
                    "SELECT payload FROM authority_grants "
                    "ORDER BY granted_at DESC, grant_id ASC"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT payload FROM authority_grants WHERE role = ? "
                    "ORDER BY granted_at DESC, grant_id ASC",
                    (role.value,),
                ).fetchall()
        return tuple(
            AuthorityGrant.model_validate(json.loads(row["payload"])) for row in rows
        )

    def insert_agent_decision(self, decision: AgentDecision) -> None:
        """Persist a validated decision. Duplicate decision_id fails closed."""
        verified = AgentDecision.model_validate(decision.model_dump(mode="json"))
        dumped = verified.model_dump(mode="json")
        reject_codes = dumped["gate_reject_codes"]
        with self._transaction():
            try:
                self._conn.execute(
                    "INSERT INTO agent_decisions ("
                    "decision_id, run_id, role, mode, environment, trade_id, "
                    "snapshot_id, action, confidence, size_multiplier, "
                    "deterministic_choice, agent_override, reason_codes, "
                    "ungrounded_codes, evidence_ids, gate_outcome, "
                    "gate_reject_codes, model_id, prompt_version, policy_version, "
                    "packet_version, input_tokens, output_tokens, latency_ms, "
                    "created_at, payload"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                    "?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        verified.decision_id,
                        verified.run_id,
                        verified.role.value,
                        verified.mode.value,
                        verified.environment.value,
                        verified.trade_id,
                        verified.snapshot_id,
                        verified.action.value,
                        (
                            None
                            if verified.confidence is None
                            else str(verified.confidence)
                        ),
                        (
                            None
                            if verified.size_multiplier is None
                            else str(verified.size_multiplier)
                        ),
                        verified.deterministic_choice,
                        int(verified.agent_override),
                        json.dumps(dumped["reason_codes"], separators=(",", ":")),
                        json.dumps(dumped["ungrounded_codes"], separators=(",", ":")),
                        json.dumps(dumped["evidence_ids"], separators=(",", ":")),
                        verified.gate_outcome.value,
                        (
                            None
                            if reject_codes is None
                            else json.dumps(reject_codes, separators=(",", ":"))
                        ),
                        verified.model_id,
                        verified.prompt_version,
                        verified.policy_version,
                        verified.packet_version,
                        verified.input_tokens,
                        verified.output_tokens,
                        verified.latency_ms,
                        _utc_iso(verified.created_at),
                        verified.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DuplicateAgentDecisionError(verified.decision_id) from exc

    def get_agent_decision(self, decision_id: str) -> AgentDecision | None:
        """Load one decision by id, or None when absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM agent_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        return AgentDecision.model_validate(json.loads(row["payload"]))

    def list_agent_decisions(
        self,
        *,
        role: DeskRole | None = None,
        model_id: str | None = None,
        prompt_version: str | None = None,
        policy_version: str | None = None,
    ) -> tuple[AgentDecision, ...]:
        """Return decisions in time order, filtered by role and/or version triple."""
        versions: tuple[str, str, str] | None = None
        if (
            model_id is not None
            or prompt_version is not None
            or policy_version is not None
        ):
            if model_id is None or prompt_version is None or policy_version is None:
                raise ValueError(
                    "model_id, prompt_version and policy_version must be supplied "
                    "together as a version triple"
                )
            versions = (model_id, prompt_version, policy_version)
        sql, params = _agent_decision_list_query(role=role, versions=versions)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return tuple(
            AgentDecision.model_validate(json.loads(row["payload"])) for row in rows
        )

    def insert_hallucination_event(self, event: HallucinationEvent) -> None:
        """Persist a grounding failure. Duplicate event_id fails closed."""
        verified = HallucinationEvent.model_validate(event.model_dump(mode="json"))
        with self._transaction():
            try:
                self._conn.execute(
                    "INSERT INTO hallucination_events ("
                    "event_id, decision_id, role, model_id, prompt_version, "
                    "created_at, payload"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        verified.event_id,
                        verified.decision_id,
                        verified.role.value,
                        verified.model_id,
                        verified.prompt_version,
                        _utc_iso(verified.created_at),
                        verified.model_dump_json(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TradingStoreError(
                    f"duplicate hallucination event_id {verified.event_id}"
                ) from exc

    def list_hallucination_events(
        self, *, decision_id: str | None = None
    ) -> tuple[HallucinationEvent, ...]:
        """Return hallucination events newest-first."""
        if decision_id is None:
            sql = (
                "SELECT payload FROM hallucination_events "
                "ORDER BY created_at DESC, event_id ASC"
            )
            params: tuple[object, ...] = ()
        else:
            sql = (
                "SELECT payload FROM hallucination_events "
                "WHERE decision_id = ? "
                "ORDER BY created_at DESC, event_id ASC"
            )
            params = (decision_id,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return tuple(
            HallucinationEvent.model_validate(json.loads(row["payload"]))
            for row in rows
        )

    def upsert_improvement_record(self, record: ImprovementRecord) -> ImprovementRecord:
        """Insert or dedupe-merge by (area, claim_key). Returns stored row."""
        key = record.claim_key or normalize_claim_key(record.area, record.claim)
        verified = ImprovementRecord.model_validate(
            {**record.model_dump(mode="json"), "claim_key": key}
        )
        with self._transaction():
            row = self._conn.execute(
                "SELECT payload FROM improvement_records "
                "WHERE area = ? AND claim_key = ?",
                (verified.area.value, verified.claim_key),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO improvement_records ("
                    "record_id, area, claim_key, status, occurrences, "
                    "estimated_cost_r, opened_at, author, payload"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        verified.record_id,
                        verified.area.value,
                        verified.claim_key,
                        verified.status.value,
                        verified.occurrences,
                        str(verified.estimated_cost_r),
                        _utc_iso(verified.opened_at),
                        verified.author.value,
                        verified.model_dump_json(),
                    ),
                )
                return verified
            existing = ImprovementRecord.model_validate(json.loads(row["payload"]))
            merged = merge_duplicate(existing, verified)
            self._conn.execute(
                "UPDATE improvement_records SET "
                "status = ?, occurrences = ?, estimated_cost_r = ?, payload = ? "
                "WHERE area = ? AND claim_key = ?",
                (
                    merged.status.value,
                    merged.occurrences,
                    str(merged.estimated_cost_r),
                    merged.model_dump_json(),
                    merged.area.value,
                    merged.claim_key,
                ),
            )
            return merged

    def list_improvement_records(
        self,
        *,
        area: ImprovementArea | None = None,
        status: ImprovementStatus | None = None,
    ) -> tuple[ImprovementRecord, ...]:
        """Return improvement records newest-first, optionally filtered."""
        clauses: list[str] = []
        params: list[object] = []
        if area is not None:
            clauses.append("area = ?")
            params.append(area.value)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT payload FROM improvement_records "
            f"{where} ORDER BY opened_at DESC, record_id ASC"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return tuple(
            ImprovementRecord.model_validate(json.loads(row["payload"])) for row in rows
        )

    def get_entry_freeze(
        self, mode_id: ModeId | None = None
    ) -> EntryFreezeRecord | None:
        """Load the persisted entry-freeze latch, if any.

        When mode_id is provided, queries mode_entry_freeze for that mode.
        Otherwise returns the singleton global freeze record.
        """
        with self._lock:
            if mode_id is not None:
                row = self._conn.execute(
                    "SELECT payload FROM mode_entry_freeze WHERE mode_id = ?",
                    (mode_id.value,),
                ).fetchone()
            else:
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
        mode_id: ModeId | None = None,
    ) -> bool:
        """Persist the freeze latch. Returns True when an audit event was written.

        When mode_id is provided (or record.mode_id is set), upserts into
        mode_entry_freeze keyed by mode_id instead of the singleton entry_freeze.

        Unchanged blocked/reason/detail combinations skip the audit append so
        restart recovery and duplicate freeze calls stay idempotent.
        """
        effective_mode_id = mode_id or record.mode_id
        existing = self.get_entry_freeze(effective_mode_id)
        unchanged = (
            existing is not None
            and existing.entries_blocked == record.entries_blocked
            and existing.reason_code == record.reason_code
            and existing.detail == record.detail
        )
        stamp = _utc_iso(recorded_at or record.updated_at)
        with self._transaction():
            if effective_mode_id is not None:
                self._conn.execute(
                    "INSERT INTO mode_entry_freeze "
                    "(mode_id, entries_blocked, reason_code, detail, payload, "
                    "updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(mode_id) DO UPDATE SET "
                    "entries_blocked = excluded.entries_blocked, "
                    "reason_code = excluded.reason_code, "
                    "detail = excluded.detail, "
                    "payload = excluded.payload, "
                    "updated_at = excluded.updated_at",
                    (
                        effective_mode_id.value,
                        1 if record.entries_blocked else 0,
                        None
                        if record.reason_code is None
                        else record.reason_code.value,
                        record.detail,
                        record.model_dump_json(),
                        stamp,
                    ),
                )
            else:
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
                        None
                        if record.reason_code is None
                        else record.reason_code.value,
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

    def list_mode_entry_freezes(self) -> tuple[EntryFreezeRecord, ...]:
        """Return all per-mode entry-freeze records."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM mode_entry_freeze ORDER BY mode_id ASC"
            ).fetchall()
        return tuple(
            EntryFreezeRecord.model_validate(json.loads(row["payload"])) for row in rows
        )

    def _initialize(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL")
        applied_at = self._clock.now_utc()
        if database_has_trading_events(self._conn):
            apply_schema_migrations(self._conn, applied_at=applied_at)
        self._conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        apply_schema_migrations(self._conn, applied_at=applied_at)

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

    def _sum_active_reservation_amount(
        self, currency: Currency, mode_id: str | None = None
    ) -> Money:
        total = Money.zero(currency)
        if mode_id is not None:
            rows = self._conn.execute(
                "SELECT payload FROM reservations WHERE mode_id = ?", (mode_id,)
            ).fetchall()
        else:
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
            "INSERT INTO reservations "
            "(reservation_id, state, mode_id, idempotency_key, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(reservation_id) DO UPDATE SET "
            "state = excluded.state, "
            "mode_id = excluded.mode_id, "
            "idempotency_key = excluded.idempotency_key, "
            "payload = excluded.payload, "
            "updated_at = excluded.updated_at",
            (
                reservation.reservation_id,
                reservation.state.value,
                reservation.mode_id.value if reservation.mode_id is not None else None,
                reservation.idempotency_key,
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


def _agent_decision_list_query(
    *,
    role: DeskRole | None,
    versions: tuple[str, str, str] | None,
) -> tuple[str, tuple[str, ...]]:
    """Static SQL for the four role/version filter combinations."""
    if versions is None:
        if role is None:
            return (
                "SELECT payload FROM agent_decisions "
                "ORDER BY created_at ASC, decision_id ASC",
                (),
            )
        return (
            "SELECT payload FROM agent_decisions WHERE role = ? "
            "ORDER BY created_at ASC, decision_id ASC",
            (role.value,),
        )
    if role is None:
        return (
            "SELECT payload FROM agent_decisions "
            "WHERE model_id = ? AND prompt_version = ? AND policy_version = ? "
            "ORDER BY created_at ASC, decision_id ASC",
            versions,
        )
    return (
        "SELECT payload FROM agent_decisions "
        "WHERE role = ? AND model_id = ? AND prompt_version = ? "
        "AND policy_version = ? ORDER BY created_at ASC, decision_id ASC",
        (role.value, versions[0], versions[1], versions[2]),
    )


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
