-- Layer 2 durable trading store (SQLite POC).
-- Append-oriented event log plus supporting tables for idempotency,
-- reservation CAS, and system readiness.

CREATE TABLE IF NOT EXISTS trading_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    idempotency_key TEXT,
    recorded_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trading_events_sequence
    ON trading_events (sequence);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT PRIMARY KEY,
    owner_ref TEXT NOT NULL,
    registered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reservations (
    reservation_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    state TEXT NOT NULL,
    last_reconciliation_ref TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS position_lifecycle (
    trade_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_slot_runs (
    slot_id TEXT NOT NULL,
    session_date TEXT NOT NULL,
    venue TEXT NOT NULL,
    as_of TEXT NOT NULL,
    PRIMARY KEY (slot_id, session_date)
);

CREATE TABLE IF NOT EXISTS entry_freeze (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    entries_blocked INTEGER NOT NULL,
    reason_code TEXT,
    detail TEXT,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS protection_state (
    trade_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_budget_ledger (
    year_month TEXT NOT NULL,
    role TEXT NOT NULL,
    spent_inr TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (year_month, role)
);

