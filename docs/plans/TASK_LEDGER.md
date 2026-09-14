# Task Ledger

ACTIVE_PLAN_VERSION: 4

Use statuses `READY`, `IN_PROGRESS`, `BLOCKED`, `DONE`. Exactly one task may be
`READY` or `IN_PROGRESS` (none; slice 1 complete).

## Phase 2–3: Layer 2 control plane

| ID | Status | Outcome | Scope | Verification | Dependency |
| --- | --- | --- | --- | --- | --- |
| L2-001 | DONE | Layer 2 contracts + deterministic replay fixtures | `domain/contracts/portfolio.py`, `sizing.py`, `reservation.py`, `order_plan.py`, `position.py`, `reconciliation_result.py`, `tests/fixtures/l2_replay/`, `tests/test_l2_contracts.py`, extend `tests/factories.py` | Strict round-trip; float/naive-datetime rejection; two replay fixtures produce byte-identical `RiskDecision` serialization | None |
| L2-002 | DONE | Durable trading event store (SQLite) | `src/trading/storage/trading_store.py`, `schema.sql`, `tests/test_trading_store.py` | Transactional append; idempotency unique constraint; recovery reads event sequence | L2-001 |
| L2-003 | DONE | Broker ports + paper adapter | `src/trading/broker/ports.py`, `broker/paper/`, `tests/fixtures/broker/`, `tests/test_paper_broker.py` | Offline fixture round-trip for orders, positions, funds, margin preview | L2-001 |
| L2-004 | DONE | Portfolio snapshot + reconciliation | `src/trading/portfolio/`, `tests/test_reconciliation.py` | Boot reconcile; critical mismatch sets `entries_blocked`; covers inv 5, 9 | L2-002, L2-003 |
| L2-005 | DONE | Atomic capital reservation | `src/trading/risk/reservation.py`, `tests/test_reservation.py` | Concurrent reserve cannot overspend; lifecycle state machine; inv 14 | L2-002 |
| L2-006 | DONE | Sizing engine (long call/put) + risk gateway | `src/trading/risk/sizing/long_option.py`, `risk/limits.py`, `risk/gateway.py`, `config/risk.yaml`, tests | `min()` constraint binding; REJECT on limit breach; confidence does not relax limits; inv 4 | L2-004, L2-005 |
| L2-007 | DONE | OrderPlan builder + OMS core | `src/trading/oms/planner.py`, `oms/engine.py`, `oms/rate_limit.py`, tests | Idempotent submit (inv 11); UNKNOWN freeze (inv 13); durable write before submit | L2-002, L2-003, L2-006 |
| L2-008 | DONE | Trade manager + deterministic exits | `src/trading/trade/manager.py`, `trade/exits.py`, tests | Stop monotonicity (inv 17); open position has protection (inv 16); partial → REPAIR_REQUIRED | L2-007 |
| L2-009 | DONE | Safety controls + readiness | `src/trading/safety/controls.py`, `safety/readiness.py`, tests | Daily loss kill switch (inv 24); stale snapshot blocks entry (inv 6); system RECOVERY gating | L2-004 |
| L2-010 | DONE | E2E vertical slice 1: long call/put paper | `tests/test_l2_slice1_long_option.py` | Full path intent→RiskDecision→OrderPlan→fill→exit→reconcile→audit; replay deterministic | L2-007, L2-008, L2-009 |

## Deferred (post slice 1)

| ID | Status | Outcome | Scope | Verification | Dependency |
| --- | --- | --- | --- | --- | --- |
| L2-011 | DONE | Slice 2: debit spread sizing + E2E | `risk/sizing/debit_spread.py`, E2E test | Defined max loss sizing; multi-leg OrderPlan | L2-010 |
| L2-012 | DONE | Slice 3: commodity futures | `risk/sizing/commodity_future.py`, E2E test | Stop-distance + margin preview sizing | L2-010 |
| L2-013 | DONE | Slice 4: credit spread / multi-leg defined risk | sizing + partial-fill repair policy | inv 15 failure tests | L2-011 |
| L2-014 | DONE | Slice 5: iron condor | sizing + combined P&L exit scope | Wing loss minus credit | L2-013 |
| L2-015 | DONE | Live Fyers broker adapter | `broker/fyers/`, `tests/fixtures/broker/fyers/`, `tests/test_fyers_broker.py` | Verified API behavior; paper parity drill | L2-010, UD-L2-03 |

## Phase 3: Layer 3 strategy systems (`feature/layer3-strategies`)

| ID | Status | Outcome | Scope | Verification | Dependency |
| --- | --- | --- | --- | --- | --- |
| L3-001 | DONE | Positional long call / long put | `src/trading/strategies/` (`base`, `macro`, `long_option`), `tests/test_l3_long_option.py` | Deterministic `TradeIntent`; ratio legs only; no lookahead; macro acceptance; eligibility rejections; mocked Layer 2 boundary | L2-001 |
| L3-002 | DONE | Vertical debit spread (bull call / bear put) | `src/trading/strategies/debit_spread.py`, `tests/test_l3_debit_spread.py` | Two ratio legs (BUY long / SELL short); strike ordering; per-leg eligibility; deterministic id | L2-001 |
| L3-003 | DONE | Directional commodity futures | `src/trading/strategies/commodity_futures.py`, `tests/test_l3_commodity_futures.py` | Single BUY/SELL leg; stop-bounded max loss; futures eligibility; deterministic id | L2-001 |
| L3-004 | DONE | Close-auction (CAS) microstructure | `src/trading/strategies/cas_microstructure.py`, `tests/test_l3_cas_microstructure.py` | Versioned feature keys; absent feature is a gap, never a default; injected-clock session-window guard; mandatory short time exit; deterministic id | L2-001 |
| L3-005 | DONE | Defined-risk multi-leg options (credit spreads) | `src/trading/strategies/multileg_options.py`, `tests/test_l3_multileg.py` | Bull put / bear call spread; strike-ordered ratio legs; short leg always covered; `ALL_OR_CANCEL`; `estimated_max_loss = requested_risk` | L2-001 |

Layer 3 slices L3-001..L3-005 are complete. An iron condor was considered for
L3-005 and deliberately left out of `defined-risk-multileg-v1`: the two-leg
credit spreads already deliver the defined-risk structure, and a four-leg
variant needs a neutral-regime rule that is not yet specified.

## Prior milestones (complete)

| ID | Status | Outcome |
| --- | --- | --- |
| NEWS-001..007 | DONE | News/macro evidence subsystem |
| L1-001..005 | DONE | Live Fyers Layer 1 completeness |

Completion record:

```text
Layer 1 live data path complete. L2-001 contracts and replay fixtures done.
L2-002 durable trading event store done. L2-003 broker ports + paper adapter done.
L2-004 portfolio snapshot + reconciliation done.
L2-005 atomic capital reservation done.
L2-006 sizing engine + risk gateway done.
L2-007 OrderPlan builder + OMS core done.
L2-008 trade manager + deterministic exits done.
L2-009 safety controls + readiness done.
L2-010 E2E vertical slice 1 (long call/put paper) done.
L2-011 debit spread sizing + E2E vertical slice 2 done.
L2-012 commodity futures sizing + E2E vertical slice 3 done.
L2-013 credit spread sizing + partial-fill repair + E2E slice 4 done.
L2-014 iron condor sizing + STRATEGY_PNL exits + E2E slice 5 done.
L2-015 live Fyers broker adapter done.
Next implementation task: none queued (see ACTIVE_PLAN.md).
```
