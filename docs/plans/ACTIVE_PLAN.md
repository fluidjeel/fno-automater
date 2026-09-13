# Active Implementation Plan

PLAN_STATUS: APPROVED
CONTEXT_DIGEST_VERSION: 4
PLANNED_AT: 2026-09-14
CONTEXT_REFRESH_REQUIRED: no

Milestone: **Phase 2–3 — Layer 2 deterministic control plane (broker truth → risk → OMS → exits)**.

## Goal

Build Layer 2 as the system's exclusive live authority for money, risk, orders, and
positions. A `TradeIntent` from Layer 3 enters; Layer 2 returns either an approved
`OrderPlan` (with reserved capital and protective coverage) or a `RiskDecision`
rejection with machine-readable reason codes. No LLM, no strategy-to-broker path.

## Scope

- **Contracts:** extend domain models for portfolio truth, sizing, reservations,
  executable order plans, position/exit runtime state, and reconciliation outcomes.
- **Broker truth:** typed broker ports, paper adapter, durable trading-event store,
  startup/reconnect reconciliation, system readiness (`RECOVERY → READY`).
- **Risk gateway:** deterministic sizing, capital reservation (atomic), pre-trade
  limits, `TradeIntent → RiskDecision`.
- **OMS:** idempotent submission, order/trade state machines, partial-fill handling,
  rate limiting, `UNKNOWN` freeze until reconciliation.
- **Trade management:** deterministic exits from versioned `ExitTemplate`, protective
  order placement policy, multi-leg repair routing.
- **Safety:** entry freeze, daily-loss kill switch, data-staleness circuit breaker,
  append-only audit trail.
- **Vertical slice 1:** premium-paid long call/put in paper mode, end to end.

## Non-goals

- Layer 3 strategy logic or market-direction selection.
- Live Fyers order submission (paper first; live adapter follows slice-1 proof).
- Naked short options, AI overrides, unbounded retries, Kelly sizing.
- Iron condor, commodity futures, CAS/microstructure (later slices).
- Full observability stack (metrics/dashboards deferred to Phase 5; audit log in scope).
- Historical option-chain vendor selection (Layer 1 blocker; does not block paper slice).

## Context digest

### Already implemented (reuse, do not rename)

| Area | Location | Notes |
| --- | --- | --- |
| Core contracts | `domain/contracts/intent.py`, `risk.py`, `order.py`, `reconciliation.py` | `TradeIntent` (not `StrategyIntent`), `RiskDecision`, `OrderEvent`, `ReconciliationEvent` |
| State machines | `domain/state/machines.py` | System, Intent, Order, Trade — extend with `ReservationState` |
| Primitives | `domain/primitives.py` | `Money`, `Price`, `Lots`, `Quantity` — no floats |
| Config limits | `config/schema.py` `RiskLimits`, `config/base.yaml` | Account policy; extend with strategy/underlying caps in `config/risk.yaml` |
| Instrument master | `domain/contracts/instrument.py`, L1 `data/fyers/symbol_master.py` | Layer 2 sizes against `InstrumentSpec` |
| Market snapshots | `domain/contracts/snapshot.py` `FeatureSnapshot` | Layer 2 reads quote/quality at decision time |
| Layer 1 pipeline | `src/trading/data/` | Quality gates, replay, catalog — Layer 2 consumes `FeatureSnapshot` + `InstrumentSpec` |
| Safety tests | `tests/test_safety_invariants.py` | Invariants 1, 5, 8, 10, 15, 24 deferred to this milestone |

### Gaps (this milestone)

- No `src/trading/broker/`, `portfolio/`, `risk/`, `oms/`, or trading event store.
- No `PortfolioSnapshot`, `CapitalReservation`, `OrderPlan`, `PositionState`, `SizingDecision`.
- `ExposureSnapshot` exists but is insufficient for broker reconciliation (no positions,
  pending orders, reservations, per-underlying exposure).
- Broker remains unverified in config; paper mode uses fixtures, not live capital.

### Authority boundaries (encoded in types)

```text
Layer 3: FeatureSnapshot + PortfolioView (read-only) → TradeIntent
Layer 2: TradeIntent + PortfolioSnapshot + FeatureSnapshot + RiskLimits
       → SizingDecision → CapitalReservation → RiskDecision → OrderPlan
       → OMS → BrokerPort → OrderEvent / fill
       → TradeState / PositionState → ExitEngine → protective orders
Broker: external truth; reconciliation rebuilds local mirror
```

`strategy_confidence` on `TradeIntent` is metadata only; it cannot relax limits.

## Package layout

```text
src/trading/
  domain/contracts/          # extend: portfolio, sizing, reservation, order_plan, position
  broker/
    ports.py                 # BrokerPort, MarginPreviewPort
    paper/                   # deterministic paper adapter + fixtures
  storage/
    trading_store.py         # durable append-only events (SQLite POC)
    schema.sql
  portfolio/
    snapshot.py              # PortfolioSnapshot builder
    view.py                  # read-only PortfolioView for Layer 3
    reconciliation.py        # compare local vs broker → ReconciliationResult
  risk/
    sizing/                  # structure-specific sizing calculators
    reservation.py           # atomic capital reservation
    gateway.py               # TradeIntent → RiskDecision orchestrator
    limits.py                # limit evaluation against config
  oms/
    planner.py               # RiskDecision → OrderPlan (executable prices)
    engine.py                # order state machine driver, submit/cancel
    rate_limit.py
  trade/
    manager.py               # trade lifecycle, PositionState
    exits.py                 # deterministic exit evaluation
  safety/
    controls.py              # kill switch, entry freeze, circuit breakers
    readiness.py             # ENTRY_READY / LIVE_SAFE evaluation
config/
  risk.yaml                  # strategy allocation, concentration, premium budget
tests/
  fixtures/broker/           # sanitized order/position/margin fixtures
  fixtures/l2_replay/          # deterministic intent→decision→order sequences
```

Import boundary: `domain/` stays infrastructure-free. `broker/`, `storage/`, `portfolio/`,
`risk/`, `oms/`, `trade/`, `safety/` may import domain + config; strategies import only
domain contracts and `PortfolioView`.

## Contracts to add or extend

| Model | Purpose | Relationship to existing |
| --- | --- | --- |
| `PortfolioSnapshot` | Broker-confirmed positions, orders, funds, margin, reservations, exposure | Superset of `ExposureSnapshot` |
| `PortfolioView` | Read-only subset exposed to Layer 3 | Derived from `PortfolioSnapshot` |
| `SizingRequest` | Intent + snapshot + limits + quote inputs | Input to sizing engine |
| `SizingDecision` | Computed lots per leg, binding constraint, margin estimate | Feeds risk gateway |
| `CapitalReservation` | Reservation lifecycle (`REQUESTED→RESERVED→COMMITTED→RELEASED`) | Linked from `RiskDecision` |
| `OrderPlan` | Approved executable orders: prices, TIF, idempotency keys, protective stubs | Output of OMS planner; input to OMS engine |
| `PositionState` | Open position: entry fills, active exit policy, protective order refs | Runtime mirror of broker position |
| `ExitPolicy` | Runtime exit state (stop level, trail active, breakeven hit) | Initialized from `ExitTemplate` |
| `ReconciliationResult` | Aggregated run outcome: diffs, `entries_blocked`, system transition | Wraps `ReconciliationEvent` list |

Do **not** add quantity or broker fields to `TradeIntent`. Do **not** rename
`TradeIntent` to `StrategyIntent`.

## State machines

### Existing (use as-is)

- **System:** `STARTING → RECOVERY → READY | DEGRADED | HALTED`
- **Intent:** `CREATED → VALIDATED → APPROVED|RESIZED|DEFERRED|REJECTED|EXPIRED`
- **Order:** `CREATED → SUBMITTING → ACKNOWLEDGED → PARTIAL → FILLED`; terminals
  `REJECTED`, `CANCELLED`, `EXPIRED`, `UNKNOWN` (reconciliation-only exit)
- **Trade:** `PENDING_ENTRY → OPENING → OPEN → EXIT_PENDING → CLOSING → CLOSED`;
  `REPAIR_REQUIRED` from any live state

### New

- **Reservation:** `REQUESTED → RESERVED | REJECTED`; `RESERVED → COMMITTED | RELEASED`;
  `COMMITTED → RELEASED`
- **OrderPlan (logical):** `CREATED → RISK_APPROVED → SUBMITTED → …` maps onto
  `OrderState` per leg; multi-leg policy in config

Illegal transitions raise `IllegalTransitionError` and emit audit evidence.

## Sizing methods (slice order)

| Slice | Structure | Initial sizing basis |
| --- | --- | --- |
| 1 | Long call/put | Premium at risk + charges/slippage buffer |
| 2 | Debit spread | Net debit, defined max loss |
| 3 | Commodity futures | Stop distance, lot value, broker margin preview |
| 4 | Credit spread / condor legs | Defined max loss + margin preview |
| 5 | Iron condor | Wing loss minus credit |
| 6 | CAS | Out of scope until slices 1–5 pass |

Core constraint (all slices):

```text
lots = min(risk_lots, capital_lots, margin_lots, portfolio_limit_lots, liquidity_lots)
```

Margin from broker preflight API where available; fail closed if unconfirmed.
Naked option selling: hard-disabled in gateway until explicit promotion.

## Database / storage

POC durable store: **SQLite** with WAL, single-writer transactional commits.

Tables (append-oriented, event-sourced recovery):

- `trading_events` — serialized `OrderEvent`, `RiskDecision`, `ReconciliationEvent`,
  `CapitalReservation` transitions (type discriminator + JSON payload + sequence)
- `reservations` — current reservation state for atomic CAS updates
- `idempotency_keys` — unique constraint; duplicate insert → `DUPLICATE_IDEMPOTENCY_KEY`
- `system_state` — current `SystemState`, last reconciliation ref

`durable_write_required_before_submit: true` (already in `config/base.yaml`) is enforced
in OMS: persist `OrderEvent(CREATED)` before `BrokerPort.submit`.

## Configuration additions (`config/risk.yaml`)

- Per-strategy capital allocation fraction
- Per-underlying concentration limit
- Asset-class allocation caps
- Directional exposure limit (net delta band)
- Options premium budget
- Correlated-position groups (manual config, no implicit correlation matrix)
- Multi-leg execution policy: `ALL_OR_CANCEL`, `HEDGE_UNWIND_ON_PARTIAL`
- Exit application scope per structure: `STRATEGY_PNL`, `LEG_PRICE`, `UNDERLYING`,
  `SPREAD_VALUE`
- Paper broker fill model: immediate full fill at limit-within-spread (slice 1)

All broker/exchange numbers remain `VerifiedValue` null until verified.

## Vertical slices and acceptance

| Slice | ID | Path | Acceptance tests |
| --- | --- | --- | --- |
| Contracts + replay fixtures | L2-001 | `domain/contracts/*`, `tests/fixtures/l2_replay/` | Round-trip; float rejected; replay produces identical `RiskDecision` bytes |
| Durable store | L2-002 | `storage/trading_store.py` | Crash-recovery; unique idempotency; transaction rollback |
| Broker ports + paper adapter | L2-003 | `broker/ports.py`, `broker/paper/` | Fixture orders/positions/margin; no network |
| Portfolio + reconciliation | L2-004 | `portfolio/` | Boot reconcile; mismatch blocks entries (inv 6, 9); inv 5 broker truth |
| Capital reservation | L2-005 | `risk/reservation.py` | Two concurrent reserves cannot overspend (inv 14) |
| Sizing + risk gateway | L2-006 | `risk/sizing/`, `risk/gateway.py` | Long call/put lots; limit breach → REJECT; confidence ignored |
| OMS + OrderPlan | L2-007 | `oms/` | Idempotent submit (inv 11); UNKNOWN blocks retry (inv 13); partial fill |
| Trade manager + exits | L2-008 | `trade/` | Stop/target/time exit; monotonic stop (inv 17); protection (inv 16) |
| Safety controls | L2-009 | `safety/` | Daily loss kill switch (inv 24); stale data blocks entry (inv 6) |
| E2E slice 1 | L2-010 | integration test | Full paper path: intent → fill → stop → close → audit lineage |

Slices 2–6 (debit spread, commodity, multi-leg, condor, CAS) each add a sizing
calculator, exit scope, and E2E test after L2-010 passes. Do not start slice 2 until
slice 1 acceptance tests are green.

## Test gates (blocking promotion to Layer 3)

- Same intent + idempotency key cannot create duplicate orders (inv 11).
- Two strategies cannot reserve the same capital (inv 14).
- Restart recovery reconstructs state from broker + event log (inv 9).
- Every order traces to `intent_id` + `risk_decision_id` (inv 25).
- Partial fills follow pre-approved policy (inv 15).
- Stale/inconsistent state blocks new entries (inv 6).
- Daily loss and margin limits cannot be bypassed via rounding (property tests).
- Replay: same snapshot + config + code → identical `RiskDecision` (inv 21).
- Failure injection: disconnect, submit timeout → `UNKNOWN`, broker mismatch freeze.

Update `tests/test_safety_invariants.py` `DEFERRED` map as each invariant gains coverage.

## Unresolved decisions (require user input before implementation)

| ID | Decision | Options | Default if silent |
| --- | --- | --- | --- |
| UD-L2-01 | POC database | SQLite (recommended) vs Postgres | SQLite |
| UD-L2-02 | Paper broker fill model | Immediate full fill vs probabilistic partial | Immediate full fill for slice 1 |
| UD-L2-03 | First broker adapter target | Paper-only first vs Fyers paper API | Paper-only fixtures first |
| UD-L2-04 | Multi-leg entry policy default | `ALL_OR_CANCEL` vs `HEDGE_UNWIND_ON_PARTIAL` | `HEDGE_UNWIND_ON_PARTIAL` per strategy spec |
| UD-L2-05 | Protective stop placement | Broker native SL-M vs local watcher supplement | Broker SL-M where Fyers supports; local watcher always as backup |

## Residual risks

- Fyers order/margin API semantics unverified; paper slice must not assume them.
- `config/base.yaml` margin `VerifiedValue` fields remain null; live margin preview
  will fail closed until verified.
- Layer 1 historical option-chain gap does not block paper slice 1 but blocks
  backtest parity for multi-leg structures.
- SQLite single-writer may need Postgres before concurrent strategy processes.
