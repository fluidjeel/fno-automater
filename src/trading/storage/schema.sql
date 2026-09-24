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
    mode_id TEXT,
    idempotency_key TEXT,
    payload TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reservations_idem
    ON reservations (idempotency_key);

CREATE TABLE IF NOT EXISTS system_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    state TEXT NOT NULL,
    last_reconciliation_ref TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS position_lifecycle (
    trade_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    mode_id TEXT,
    campaign_id TEXT,
    policy_version TEXT,
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

CREATE TABLE IF NOT EXISTS mode_entry_freeze (
    mode_id TEXT PRIMARY KEY,
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

CREATE TABLE IF NOT EXISTS session_protection (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
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

-- Operator-signed Agent Desk grants. Agents never write this table.
CREATE TABLE IF NOT EXISTS authority_grants (
    grant_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    mode TEXT NOT NULL,
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    environment TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    signed_by TEXT NOT NULL,
    checksum TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_authority_grants_role_valid
    ON authority_grants (role, valid_until);

-- Append-only Agent Desk decision log. Analytics substrate; never a live instruction.
CREATE TABLE IF NOT EXISTS agent_decisions (
    decision_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    role TEXT NOT NULL,
    mode TEXT NOT NULL,
    environment TEXT NOT NULL,
    trade_id TEXT,
    snapshot_id TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence TEXT,
    size_multiplier TEXT,
    deterministic_choice TEXT,
    agent_override INTEGER NOT NULL,
    reason_codes TEXT NOT NULL,
    ungrounded_codes TEXT NOT NULL,
    evidence_ids TEXT NOT NULL,
    gate_outcome TEXT NOT NULL,
    gate_reject_codes TEXT,
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    packet_version TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_decisions_role_created
    ON agent_decisions (role, created_at);

CREATE INDEX IF NOT EXISTS idx_agent_decisions_versions_created
    ON agent_decisions (model_id, prompt_version, policy_version, created_at);

CREATE INDEX IF NOT EXISTS idx_agent_decisions_created
    ON agent_decisions (created_at);



CREATE TABLE IF NOT EXISTS improvement_records (
    record_id TEXT PRIMARY KEY,
    area TEXT NOT NULL,
    claim_key TEXT NOT NULL,
    status TEXT NOT NULL,
    occurrences INTEGER NOT NULL,
    estimated_cost_r TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    author TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_improvement_records_area_claim
    ON improvement_records (area, claim_key);

CREATE INDEX IF NOT EXISTS idx_improvement_records_status
    ON improvement_records (status, opened_at);


CREATE TABLE IF NOT EXISTS hallucination_events (
    event_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL,
    role TEXT NOT NULL,
    model_id TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hallucination_events_decision
    ON hallucination_events (decision_id, created_at);
