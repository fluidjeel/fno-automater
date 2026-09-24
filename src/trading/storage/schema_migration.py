"""Transactional, idempotent schema migrations for legacy trading stores."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trading.storage.trading_store import TradingStore

__all__ = [
    "MIGRATION_PRE_900293F_FILL_CHARGES",
    "MIGRATION_PRE_900293F_SCHEMA",
    "RowCounts",
    "apply_data_migrations",
    "apply_schema_migrations",
    "capture_row_counts",
    "database_has_trading_events",
    "is_migration_applied",
    "migration_row_counts",
]

MIGRATION_PRE_900293F_SCHEMA = "pre_900293f_schema"
MIGRATION_PRE_900293F_FILL_CHARGES = "pre_900293f_fill_charges"


@dataclass(frozen=True, slots=True)
class RowCounts:
    """Table row counts used to prove migrations preserve existing data."""

    trading_events: int
    position_lifecycle: int
    reservations: int
    campaign_ledger: int
    fill_charges: int
    idempotency_keys: int
    schema_migrations: int


def database_has_trading_events(connection: sqlite3.Connection) -> bool:
    """Return whether this file already has the durable event log table."""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trading_events'"
    ).fetchone()
    return row is not None


def capture_row_counts(connection: sqlite3.Connection) -> RowCounts:
    """Snapshot row counts for every table touched by pre-900293f migrations."""
    return RowCounts(
        trading_events=_count_rows(connection, "trading_events"),
        position_lifecycle=_count_rows(connection, "position_lifecycle"),
        reservations=_count_rows(connection, "reservations"),
        campaign_ledger=_count_rows(connection, "campaign_ledger"),
        fill_charges=_count_rows(connection, "fill_charges"),
        idempotency_keys=_count_rows(connection, "idempotency_keys"),
        schema_migrations=_count_rows(connection, "schema_migrations"),
    )


def migration_row_counts(connection: sqlite3.Connection) -> RowCounts:
    """Alias for before/after migration comparisons in tests."""
    return capture_row_counts(connection)


def is_migration_applied(connection: sqlite3.Connection, migration_id: str) -> bool:
    """Return whether a migration id is already recorded."""
    _ensure_schema_migrations_table(connection)
    row = connection.execute(
        "SELECT 1 FROM schema_migrations WHERE migration_id = ?",
        (migration_id,),
    ).fetchone()
    return row is not None


def apply_schema_migrations(
    connection: sqlite3.Connection,
    *,
    applied_at: datetime,
) -> tuple[str, ...]:
    """Apply DDL migrations inside one transaction; return applied ids."""
    _ensure_schema_migrations_table(connection)
    if is_migration_applied(connection, MIGRATION_PRE_900293F_SCHEMA):
        return ()
    connection.execute("BEGIN IMMEDIATE")
    try:
        _migrate_pre_900293f_schema(connection)
        _record_migration(connection, MIGRATION_PRE_900293F_SCHEMA, applied_at=applied_at)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return (MIGRATION_PRE_900293F_SCHEMA,)


def apply_data_migrations(store: TradingStore) -> tuple[str, ...]:
    """Apply idempotent data backfills after schema is current."""
    connection = store._conn
    _ensure_schema_migrations_table(connection)
    if is_migration_applied(connection, MIGRATION_PRE_900293F_FILL_CHARGES):
        return ()
    _migrate_pre_900293f_fill_charges(connection, store=store)
    applied_at = store._clock.now_utc()
    with store._transaction():
        _record_migration(
            connection,
            MIGRATION_PRE_900293F_FILL_CHARGES,
            applied_at=applied_at,
        )
    return (MIGRATION_PRE_900293F_FILL_CHARGES,)


def _migrate_pre_900293f_schema(
    connection: sqlite3.Connection,
    *,
    store: TradingStore | None = None,
) -> None:
    """Bring pre-900293f stores forward without mutating existing event rows."""
    _add_column_if_missing(connection, "reservations", "mode_id", "TEXT")
    _add_column_if_missing(connection, "reservations", "idempotency_key", "TEXT")
    _add_column_if_missing(connection, "position_lifecycle", "mode_id", "TEXT")
    _add_column_if_missing(connection, "position_lifecycle", "campaign_id", "TEXT")
    _add_column_if_missing(connection, "position_lifecycle", "policy_version", "TEXT")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_reservations_idem "
        "ON reservations (idempotency_key)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS campaign_ledger ("
        "campaign_id TEXT PRIMARY KEY, "
        "mode_id TEXT NOT NULL, "
        "payload TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS fill_charges ("
        "fill_idempotency_key TEXT PRIMARY KEY, "
        "trade_id TEXT NOT NULL, "
        "payload TEXT NOT NULL, "
        "updated_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_fill_charges_trade_id "
        "ON fill_charges (trade_id)"
    )


def _migrate_pre_900293f_fill_charges(
    connection: sqlite3.Connection,
    *,
    store: TradingStore | None = None,
) -> None:
    """Backfill durable per-fill charges for historical broker-confirmed fills."""
    if store is None:
        return
    from trading.config.charge_policy import load_charge_policy
    from trading.portfolio.fill_charge_recorder import backfill_fill_charges
    from trading.portfolio.fill_ledger import index_order_events

    policy = load_charge_policy().config
    orders = index_order_events(store)
    contracts_by_trade: dict[str, int] = {}
    default_lot = policy.default_contracts_per_lot
    for lifecycle in store.list_position_lifecycle():
        approved = lifecycle.risk_decision.approved_legs
        if approved:
            contracts_by_trade[lifecycle.trade_id] = (
                approved[0].lot_size.contracts_per_lot
            )
    backfill_fill_charges(
        store,
        orders,
        policy=policy,
        contracts_per_lot_by_trade=contracts_by_trade,
        default_contracts_per_lot=default_lot,
    )


def _ensure_schema_migrations_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "migration_id TEXT PRIMARY KEY, "
        "applied_at TEXT NOT NULL)"
    )


def _record_migration(
    connection: sqlite3.Connection,
    migration_id: str,
    *,
    applied_at: datetime,
) -> None:
    connection.execute(
        "INSERT INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
        (migration_id, applied_at.isoformat()),
    )


def _add_column_if_missing(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    col_type: str,
) -> None:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        return
    existing = {row[1] for row in rows}
    if column not in existing:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def _count_rows(connection: sqlite3.Connection, table: str) -> int:
    if not _table_exists(connection, table):
        return 0
    row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row is not None else 0


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None
