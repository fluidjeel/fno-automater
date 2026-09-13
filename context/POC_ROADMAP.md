# POC Roadmap

Build one safe vertical path before adding strategy breadth.

## Phase 0: Repository and contracts

- Package structure, configuration validation, injected clock/IDs.
- Domain contracts, enums, money/quantity types and state machines.
- CI for formatting, typing and tests.

Exit: schemas round-trip, invalid values fail closed and core invariants have tests.

## Phase 1: Data and replay

- One market feed adapter plus instrument/reference data.
- Canonical events, normalization, quality/freshness and feature snapshots.
- Raw/canonical storage and deterministic replay.
- Minimal calculations needed by the POC strategy.

Exit: recorded session replays identically without time/data leakage.

## Phase 2: Broker truth and recovery

- Paper broker adapter and sanitized contract fixtures.
- Durable trading events.
- Startup/reconnect reconciliation and system readiness state machine.

Exit: restart, disconnect, unknown order and mismatch scenarios pass.

## Phase 3: Layer 2 control plane

- Portfolio view and capital reservation.
- Sizing/allocation and RiskDecision.
- OMS, trade lifecycle, deterministic exits and safety controls.

Exit: no direct strategy-to-broker path; partial fills, idempotency, stops, kill
switch and recovery are tested.

## Phase 4: First strategy end to end

- Positional defined-risk index option strategy.
- Snapshot -> TradeIntent -> RiskDecision -> paper order -> fill -> protection ->
  deterministic close -> reconciliation/audit.

Exit: shadow/paper parity and complete trace from evidence to close.

## Phase 5: Observability and failure drills

- Readiness, metrics, structured audit, dashboards and actionable alerts.
- Automated bounded recovery and documented drills.

Exit: live-safe operation demonstrated with AI disabled and injected failures.

## Phase 6: Layer 4 AI support

- Deterministic macro/news preprocessing and allowlisted evidence.
- Weekly bounded macro/regime/strategy-family proposal.
- Universe ranking after deterministic eligibility filters.
- Post-market evaluator and evidence package.
- Offline promotion/rollback mechanism.

Exit: malformed, injected, stale, contradictory and unavailable AI outputs cannot
affect live safety or bypass promotion.

## Phase 7: Additional strategies

Add each independently behind existing contracts with its own specification,
research evidence, failure policies and release gates.

## POC constraints

- Approximately INR 5-7 lakh assumption; actual caps are evidence-based config.
- Defined-risk and capped fractional sizing first; Kelly never overrides hard caps.
- Modular monolith/process isolation before microservices.
- Persistent safety core on VPS; asynchronous analytics in batch/serverless.

