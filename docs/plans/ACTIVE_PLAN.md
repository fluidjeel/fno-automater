# Active Implementation Plan

PLAN_STATUS: APPROVED
CONTEXT_DIGEST_VERSION: 10
PLANNED_AT: 2026-09-20
CONTEXT_REFRESH_REQUIRED: no

Milestone: **Agent Desk Stage A — foundations (zero LLM calls).**

## Goal

Land the deterministic contracts and measurement substrate the Agent Desk needs
before any desk runtime or LLM path: authority grants (with C1 baked in),
decision log, trade theses + invalidation, exposure/stress reports, bias battery,
improvement records, grounded-reason validation, and versioned packets.

Stage A ships **no LLM calls** and **no live-path agent influence**. If we stopped
after Stage A, paper trading and risk measurement would still be strictly better.

## Binding decisions (do not re-litigate in implementation)

- **C1 (2026-09-20):** `AuthorityMode.BOUNDED` = **config-promotion only**.
  Intraday stays L3 + L2 with **no LLM**. A BOUNDED grant may only allow actions
  that propose enable/shadow/halt of already-coded strategy families via the L4
  proposal path. It must **reject** grants that name live-path actions (size,
  stop, submit, veto-as-order, tighten, partial exit, roll, hedge, add).
- **C2:** Invariant 21 is **not** amended; L4 replay is via stored artifacts.
- **C3:** Schedule `verified_at` accepted for paper; LIVE still needs contract-note
  cross-check.
- Stage 0 (ADESK-A0.1..A0.6) is **DONE** on `main` (`24435de`).

## Nested cadence (unchanged)

1. **Intraday** — `trading paper session`: L3 + L2 only. No LLM.
2. **Pre-open day** — Telegram OAuth + readiness.
3. **Week / period** — existing L4 weekly/advise loops only; still default-off.

## Build order (one PR per ledger row; do not reorder)

| ID | Slice | Acceptance (summary) |
| --- | --- | --- |
| ADESK-A1 | Enums + `AuthorityGrant` + `authority_grants` + demotion | No/expired/mismatched grant → OBSERVE; BOUNDED grant with live-path action → reject at write; config-promotion actions only when BOUNDED |
| ADESK-A2 | `agent_decisions` + `DecisionLog` | Round-trip write/read; query by role + versions |
| ADESK-A3 | `TradeThesis` + invalidation evaluator | Golden fixtures for every `InvalidationMetric` |
| ADESK-A4 | `ExposureReport` + `risk.yaml` limits | Matrix tests; gateway rejects each new limit |
| ADESK-A5 | `StressReport` + `assume_no_fills` | Debit-spread worst case = net debit; budget breach freezes entry |
| ADESK-A6 | `analytics/bias.py` battery | All 11 metrics on a fixture cohort (needs A2) |
| ADESK-A7 | `ImprovementRecord` + table + clustering | `trading evaluate improvements` ranks clusters |
| ADESK-A8 | Reason preconditions + `hallucination_events` | Ungrounded code → ABSTAIN + event (needs A2) |
| ADESK-A9 | Versioned packets + `delta_gap_rate` | Golden packets; prefix-stability hash test |

## C1 enforcement in A1 (non-negotiable)

When implementing ADESK-A1:

1. Closed enums: `DeskRole`, `AuthorityMode` (OBSERVE / SHADOW / ADVISORY / BOUNDED),
   `AgentAction`.
2. Partition `AgentAction` into **config-promotion** vs **live-path** (and any
   advisory-only). BOUNDED grants may list **only** config-promotion actions.
3. Persist grants in `authority_grants`; load path demotes to OBSERVE on missing,
   expired, or `(model_id, prompt_version, policy_version)` triple mismatch.
4. **No auto-renewal. No agent-written grants.** Only operator/signed insert.
5. Unit tests must include a BOUNDED grant that tries `TIGHTEN_STOP` / `VETO_ENTRY`
   as order-path actions and is **rejected**.

## Current foundation (already on main)

- Dashboard, monthly `agent_budget_ledger`, temp=0/seed/resolved model + artifacts,
  separate setup vs agent Brier, paper `charges_per_lot.verified_at`.
- ADESK-A1: `AuthorityGrant`, `authority_grants`, demotion to OBSERVE. BOUNDED
  accepts only FamilyStance config-promotion actions; live-path and LIVE+BOUNDED
  are rejected at validate/write.
- ADESK-A2: `AgentDecision`, append-only `agent_decisions`, `DecisionLog` writer.
  Queryable by role and `(model_id, prompt_version, policy_version)`.
- L4 weekly `STRATEGY_FAMILY` proposals (`AIProposal`) — the only BOUNDED
  surface under C1.
- Paper session with software stops (still not live-safe).

## Remaining work this plan

ADESK-A2 is DONE. Implement ADESK-A3 through ADESK-A9 as separate PRs in order.
After A9, close this plan and open a Stage B planning pass (SHADOW desks). Do
not start Stage B code in this plan.

## Blocking gaps (outside Stage A code)

- Broker-resident protective orders / sub-60s protection (highest live-safety gap).
- Monday ops: Fyers auth + CAS depth benchmark + supervised paper session.
- Stage D live veto/tighten-as-BOUNDED is **out of scope forever under C1**; Stage D
  will be re-planned as config-promotion BOUNDED + SHADOW/ADVISORY for the rest.

## Acceptance (plan done when)

- All ADESK-A1..A9 are DONE with green `ruff` / `mypy` / `pytest`.
- Zero LLM calls added on the intraday path.
- A1 tests prove C1: BOUNDED cannot carry live-path actions.
- CURRENT_STATE Verification cites Stage A evidence.

## Non-goals

- Any desk runtime, SHADOW/ADVISORY LLM loop, or Stage B+ code.
- Amending “intraday never LLM” or granting BOUNDED for veto/tighten/size.
- Touching OMS/broker submit paths except new **reject** reasons for exposure/stress.
- Auto-renewal of grants; agents writing grants; parsing `narrative`.
