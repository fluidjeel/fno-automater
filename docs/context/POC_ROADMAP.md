# POC Roadmap

Build one safe vertical path before adding strategy breadth.

## Phase 0: Repository and contracts — **DONE**

- Package structure, configuration validation, injected clock/IDs.
- Domain contracts, enums, money/quantity types and state machines.
- CI for formatting, typing and tests.

Exit: schemas round-trip, invalid values fail closed and core invariants have tests.

## Phase 1: Data and replay — **DONE** (historical option-chain vendor TBD)

- One market feed adapter plus instrument/reference data.
- Canonical events, normalization, quality/freshness and feature snapshots.
- Raw/canonical storage and deterministic replay.
- Minimal calculations needed by the POC strategy.

Exit: recorded session replays identically without time/data leakage.

**Open:** point-in-time historical option-chain source for multi-leg backtests.

## Phase 2: Broker truth and recovery — **DONE**

- Paper broker adapter and sanitized contract fixtures.
- Fyers live broker adapter (offline fixture tests; live drill pending).
- Durable trading events.
- Startup/reconnect reconciliation and system readiness state machine.

Exit: restart, disconnect, unknown order and mismatch scenarios pass (paper).

## Phase 3: Layer 2 control plane — **DONE**

- Portfolio view and capital reservation.
- Sizing/allocation and RiskDecision (six structure calculators).
- OMS, trade lifecycle, deterministic exits and safety controls.
- Five paper E2E vertical slices (long option through iron condor).

Exit: no direct strategy-to-broker path; partial fills, idempotency, stops, kill
switch and recovery are tested.

## Phase 4: First strategy end to end — **IN PROGRESS**

- Positional defined-risk index option strategy and additional plug-ins.
- Snapshot -> TradeIntent -> RiskDecision -> paper order -> fill -> protection ->
  deterministic close -> reconciliation/audit.

**Done:** Layer 3 strategies L3-001..L3-005; L3→L2 integration tests;
PAPER-001 supervised paper runner with PAPER/live isolation; PAPER-002 optional
conservative fills on the paper broker.

**Remaining:** live Fyers shadow/paper path; PAPER-003/004 evidence store and
drills; PAPER-005 human promotion record. Historical backtest is not a gate.

Exit: shadow/paper parity and complete trace from evidence to close.

## Phase 5: Observability and failure drills — **NOT STARTED**

- Readiness, metrics, structured audit, dashboards and actionable alerts.
- Automated bounded recovery and documented drills.

Exit: live-safe operation demonstrated with AI disabled and injected failures.

## Phase 6: Layer 4 forward validation — **IN PROGRESS**

- Experiment identity, conservative fill calculator, deterministic scorecard and
  fail-closed eligibility on frozen offline cohorts (L4-001..L4-004 done).
- Operator attention contract + CLI/Telegram (L4-HUMAN-001 done).
- Bounded weekly tool loop emitting expiring `STRATEGY_FAMILY` proposals
  (L4-AGENT-001 done). Default `config/agent.yaml enabled: false`; no LLM on the
  live path. Scorecards remain authoritative P&L.
- Later: production Anthropic client behind `LlmPort`, champion–challenger
  routing of already-approved IDs, OCI Function packaging, heartbeat watchdog.

Exit: malformed, injected, stale, contradictory, timed-out and over-budget AI
outputs cannot affect live safety or bypass promotion. Eligibility reports
cannot deploy.

## Phase 7: Additional strategies — **IN PROGRESS**

CAS `cas-microstructure-v1` keys are produced in Layer 1 from timestamped depth
(CAS-001 feature producer done). SHADOW/PAPER CAS still needs a live close-window
cohort; constructed tests are not promotion evidence. Other plug-ins stay behind
existing contracts with their own specifications and gates.

## POC constraints

- Approximately INR 5-7 lakh assumption; actual caps are evidence-based config.
- Defined-risk and capped fractional sizing first; Kelly never overrides hard caps.
- Modular monolith/process isolation before microservices.
- Persistent safety core on VPS; asynchronous analytics in batch/serverless.
