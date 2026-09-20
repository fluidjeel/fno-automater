# Task Ledger

ACTIVE_PLAN_VERSION: 10

Use statuses `READY`, `IN_PROGRESS`, `BLOCKED`, `DONE`. Next READY: ADESK-A1 (Stage A plan approved; C1 = config-promotion only).

## Agent Desk Stage 0 — measurement prerequisites (PART 16 / PART 15 A0)

| ID | Status | Outcome | Scope | Verification | Dependency |
| --- | --- | --- | --- | --- | --- |
| ADESK-A0.1 | DONE | Land dashboard so HEAD matches CI | `src/trading/dashboard/`, `tests/test_dashboard.py`, `docs/context/DASHBOARD.md`; remove WIP mypy ignore | Clean checkout: `uv run mypy` + `trading dashboard snapshot` | None |
| ADESK-A0.2 | DONE | Reconcile CURRENT_STATE with fresh evidence | `docs/context/CURRENT_STATE.md` Verification + charges + dashboard | Claims match `uv run mypy` / `uv run pytest` on committed tree | ADESK-A0.1 |
| ADESK-A0.3 | DONE | Persist agent budget per `(year_month, role)` | `budget.py`, `agent_budget_ledger` in `schema.sql`, `TradingStore`, loop/advise | Test: two runs share monthly cap; exhaustion → ABSTAIN, never blocks trading | None |
| ADESK-A0.4 | DONE | Resolved model id, temperature 0, seed, full run persistence | `openai_compat.py`, `recording.py`, `ModelVersions` wiring | Recorded run: `ModelVersions.model` = provider-returned id; request has temperature/seed | None |
| ADESK-A0.5 | DONE | Score agent confidence + Brier reliability | `judgment.py`; separate agent vs setup Brier | Report shows agent Brier distinct from setup Brier; reliability component present | None |
| ADESK-A0.6 | DONE | Charges authenticity + doc alignment | `config/evaluation.yaml`, attention, CURRENT_STATE/ACTIVE_PLAN | Schedule `verified_at` accepted for paper; docs consistent; 60s stops remain documented blocker | None |

## Agent Desk Stage A — foundations (zero LLM; plan v10)

| ID | Status | Outcome | Scope / verification | Dependency |
| --- | --- | --- | --- | --- |
| ADESK-A1 | READY | Authority enums + `AuthorityGrant` + demotion | `DeskRole`/`AuthorityMode`/`AgentAction`; `authority_grants`; no/expired/triple-mismatch → OBSERVE; BOUNDED rejects live-path actions (C1) | All ADESK-A0.*; C1 |
| ADESK-A2 | BLOCKED | `agent_decisions` + `DecisionLog` | Round-trip; query by role + model/prompt/policy versions | ADESK-A1 |
| ADESK-A3 | BLOCKED | `TradeThesis` + invalidation evaluator | `analytics/invalidation.py`; golden fixtures per `InvalidationMetric` | ADESK-A1 |
| ADESK-A4 | BLOCKED | `ExposureReport` + risk limits | `portfolio/exposure.py` + `risk.yaml`; gateway reject matrix | ADESK-A1 |
| ADESK-A5 | BLOCKED | `StressReport` + `assume_no_fills` | Debit worst case = net debit; entry freeze on budget breach | ADESK-A1 |
| ADESK-A6 | BLOCKED | `analytics/bias.py` battery | All 11 metrics on fixture cohort | ADESK-A2 |
| ADESK-A7 | BLOCKED | `ImprovementRecord` + clustering | Table + dedupe; `trading evaluate improvements` | ADESK-A1 |
| ADESK-A8 | BLOCKED | Reason preconditions + hallucinations | Ungrounded → ABSTAIN + `hallucination_events` | ADESK-A2 |
| ADESK-A9 | BLOCKED | Versioned packets + `delta_gap_rate` | Golden packets; prefix-stability hash | ADESK-A1 |

## Agent Desk Stage B — desks in SHADOW (BLOCKED until Stage A complete)

| ID | Status | Outcome | Dependency |
| --- | --- | --- | --- |
| ADESK-B1 | BLOCKED | Role-based runtime; migrate weekly/advise | Stage A complete |
| ADESK-B2 | BLOCKED | ENTRY desk + StrikeShortlist + EntryAdvice SHADOW | Stage A complete |
| ADESK-B3 | BLOCKED | POSITION desk + delta packet SHADOW | Stage A complete |
| ADESK-B4 | BLOCKED | Review-level labelling in judgment | Stage A complete |
| ADESK-B5 | BLOCKED | Cold-review scheduler + warm_cold_divergence | Stage A complete |
| ADESK-B6 | BLOCKED | PORTFOLIO desk SHADOW | Stage A complete |
| ADESK-B7 | BLOCKED | MACRO desk + MacroCalendar + injection suite | Stage A complete |
| ADESK-B8 | BLOCKED | POSTTRADE desk + TradeAttribution | Stage A complete |
| ADESK-B9 | BLOCKED | FRAGILITY desk ADVISORY | Stage A complete |
| ADESK-B10 | BLOCKED | agent_scorecard.py + CLI | Stage A complete |

## Agent Desk Stage C — terminal policy (BLOCKED until Stage A complete)

| ID | Status | Outcome | Dependency |
| --- | --- | --- | --- |
| ADESK-C1 | BLOCKED | TerminalPolicy contracts + eligibility gate | Stage A complete |
| ADESK-C2 | BLOCKED | Freeze terminal policy into ExitPolicy at entry | ADESK-C1 |
| ADESK-C3 | BLOCKED | Deterministic continuous enforcement + one-way revert | ADESK-C2 |
| ADESK-C4 | BLOCKED | Terminal-policy cost accounting in scorecard | ADESK-C3 |

## Agent Desk Stage D — authority ladder (BLOCKED until Stage B/C; C1 = config-promotion only)

| ID | Status | Outcome | Dependency |
| --- | --- | --- | --- |
| ADESK-D1 | BLOCKED | Confidence-bucket enum + Phase-1 downscale-only sizing | Stage B complete |
| ADESK-D2 | BLOCKED | Promote FRAGILITY and POSTTRADE to ADVISORY | Stage B complete |
| ADESK-D3 | BLOCKED | Promote PORTFOLIO to BOUNDED for config-promotion proposals only (C1) | Stage B complete; C1=config-promotion |
| ADESK-D4 | BLOCKED | Re-scope POSITION desk: SHADOW/ADVISORY only for tighten/partial (not BOUNDED) | Stage B complete; C1 forbids live-path BOUNDED |
| ADESK-D5 | BLOCKED | Re-scope ENTRY desk: SHADOW/ADVISORY for veto/reduce; BOUNDED only if config-promotion | Stage B complete; C1 |
| ADESK-D6 | BLOCKED | Phase-2 upscale unlock (deterministic envelope only; never agent BOUNDED) | Stage D prerequisites; C1 |

## Agent Desk Stage E — research loop (BLOCKED until Stage D prerequisites)

| ID | Status | Outcome | Dependency |
| --- | --- | --- | --- |
| ADESK-E1 | BLOCKED | RESEARCH desk weekly | Stage D prerequisites |
| ADESK-E2 | BLOCKED | Hypothesis → experiment promotion ladder | ADESK-E1 |
| ADESK-E3 | BLOCKED | Monthly meta-report | ADESK-E1 |

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

## Additional Layer 2 vertical slices

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

## Phase 4: Layer 4 forward validation (offline first)

| ID | Status | Outcome | Scope | Verification | Dependency |
| --- | --- | --- | --- | --- | --- |
| L4-001 | DONE | Experiment identity + lineage stamps | `ExecutionMode`, `ExperimentDefinition`; `experiment_id`/`execution_mode` on `TradeIntent`, `OrderIdentity`, `RiskDecision`, `PositionState` | Frozen-after-start; REAL modes require `Environment.LIVE`; inv 22 | L3-001 |
| L4-002 | DONE | Conservative fill calculator | `src/trading/analytics/fills.py`, `config/evaluation.yaml` | Bid/ask/depth rules; unverified charges fail closed; paper broker E2E unchanged | L4-001 |
| L4-003 | DONE | Deterministic scorecard | `src/trading/analytics/scorecard.py`, `tests/fixtures/l4_cohort/` | Long-option cohort including rejects; no cross-version pooling | L4-002 |
| L4-004 | DONE | Fail-closed eligibility + read-only CLI | `PromotionEligibilityResult`, `trading evaluate scorecard/eligibility` | Cannot write live config; win rate / gross P&L never pass; fixture `ELIGIBLE` is pipeline-only | L4-003 |

## Phase 4–5: Forward paper validation and live readiness

| ID | Status | Outcome | Verification |
| --- | --- | --- | --- |
| SAFE-001 | DONE | Noise threshold, missing-OI fail-close, event-risk enforcement, L3 allocation coverage and CAS feature-version gate | Ruff and mypy clean; 819 tests pass, 5 skip |
| L4-001..004 | DONE | Forward-validation contracts, conservative fills, scorecard, eligibility CLI | 853 tests pass, 5 skip; inv 22 covered |
| PAPER-001 | DONE | Supervised PAPER runner joining L1 snapshots, event risk, L3, L2 and paper OMS with credential isolation | No live broker submit path in PAPER; cycle lineage; `trading paper isolate-check` |
| PAPER-002 | DONE | Conservative fill model optional on the paper broker | Immediate fill remains default for L2 E2E; conservative path uses ask+slip / trade-through / depth |
| L4-HUMAN-001 | DONE | `AttentionRequest` + CLI/Telegram for charges, CAS features, LIVE config | Advisory only; `trading attention scan` |
| L4-AGENT-001 | DONE | Bounded weekly tool loop, `STRATEGY_FAMILY` proposal, cost/iteration caps | Timeout, injection prefix, budget abort → `ABSTAIN`; `enabled: false` |
| PAPER-003 | DONE | Versioned paper evidence store and cohort report | EOD writes `CohortPackage` JSON under `data/paper/cohorts/`; `trading evaluate scorecard|eligibility` remains read-only |
| PAPER-004 | DONE | Isolation, stale/event-risk, restart idempotency, Telegram advisory copy | `tests/test_paper_session.py`; Fyers txn adapter still refused |
| PAPER-006 | DONE | PAPER positional lifecycle persistence and restart recovery | `tests/test_paper_lifecycle.py`; software-only exits, 60s poll, no broker-resident PAPER stops |
| PAPER-007 | DONE | Twice-daily PAPER positional review (10:30/14:30 IST) against persisted frozen policy | `tests/test_paper_review.py`; HOLD/TIGHTEN/PARTIAL/FULL; hedge/roll proposal-only; missed-slot restart |
| PAPER-008 | DONE | Observational 12-case PAPER positional e2e sim (no production patch) | `tests/paper_positional_sim/`; debit-spread SNAPSHOT_MISMATCH recorded as P0 |
| PAPER-009 | DONE | P0 safety hardening: multi-leg quote bundle, persistent entry freeze, missing-monitor UNPROTECTED_POSITION, stale PROTECTION_DEGRADED | 75 focused tests pass; ruff/mypy clean; 60s poll is SAFETY not OK for live; unattended PAPER is not claimed ready |
| PAPER-010 | DONE | Two-tier PaperDataRequirements: P0 fail-closed entry gate, observed-only P1 selection | `tests/test_paper_data_requirements.py`; ruff/mypy; LIVE still off |
| PAPER-011 | DONE | Full P1 builders (IV surface/skew/term, RV, greeks, depth) wired into ranking, router, allow-table, and exit observation | `tests/test_paper_data_requirements.py`; ruff/mypy; LIVE still off; families not auto-ENABLED |
| PAPER-005 | BLOCKED | Minimal-capital promotion record and rollback plan | Requires completed paper evidence and verified LIVE configuration |
| CAS-001 | DONE | Produce and quality-gate `cas-microstructure-v1` in Layer 1 | All four keys from depth; version stamped only when complete; live SHADOW still PAPER-003 |

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
Layer 2 milestone complete. Layer 3 strategies L3-001..L3-005 done.
SAFE-001 paper-readiness constraint hardening done.
L4-001..L4-004 forward-validation slice done (experiment, fills, scorecard,
eligibility CLI). Offline fixture ELIGIBLE is not a go-live.
PAPER-001 supervised paper runner with isolation done.
PAPER-002 conservative paper fills (opt-in) done.
L4-HUMAN-001 operator attention and L4-AGENT-001 weekly loop done.
CAS-001 Layer 1 cas-microstructure-v1 producer done; live CAS cohort is next.
PAPER-003 versioned paper evidence store done.
PAPER-004 isolation/stale/event-risk/restart drills done.
PAPER-006 PAPER positional lifecycle persistence/recovery done.
  Debit spreads keep frozen LEG_PRICE on the monitor long; not STRATEGY_PNL.
PAPER-007 twice-daily PAPER positional review (NSE 10:30/14:30 IST) done.
  Missed slots replay once before EOD; HEDGE/ROLL are L2 proposals (not auto-submitted).
  Protective coverage remains software-only between 60s polls.
Identification stack (market state, binders, router) wired into paper session.
L4 weekly agent DeepSeek OpenAI-compat client landed; config/agent.yaml enabled:false.
PAPER-010 two-tier paper-data contract: P0 gates paper entry; P1 ranking is
observed-only (no invented IV/skew/term/depth).
Agent Desk Stage 0 complete (ADESK-A0.1..A0.6): dashboard landed, budget ledger,
model lineage, agent Brier, charges/docs aligned.
Next: implement ADESK-A1 (AuthorityGrant + C1 demotion); Monday Fyers + CAS depth + supervised paper; PAPER-005 after live paper evidence.
```
