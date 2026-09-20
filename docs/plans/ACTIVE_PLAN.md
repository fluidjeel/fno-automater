# Active Implementation Plan

PLAN_STATUS: APPROVED
CONTEXT_DIGEST_VERSION: 9
PLANNED_AT: 2026-09-20
CONTEXT_REFRESH_REQUIRED: no

Milestone: **Agent Desk Stage 0 — measurement prerequisites (no desk yet).**

## Goal

Make Layer 4 measurement honest before any Agent Desk feature work: green
committed CI for the dashboard import, truthful CURRENT_STATE, persisted
token budget, reproducible agent run lineage, agent-confidence calibration
scoring, and charges/docs aligned with a conscious verification decision.
Stage 0 plan is closed. Stage A is a separate planning pass (C1 resolved).

## Nested cadence (unchanged; do not collapse)

1. **Intraday** — `trading paper session`: L3 + L2 only. No LLM.
2. **Pre-open day** — Telegram OAuth + readiness. No LLM unless AttentionRequest.
3. **Week / period** — existing `trading agent weekly` / `advise` only; still
   `enabled: false` by default. Stage 0 hardens measurement around these loops.

## Current foundation

- L4 weekly + advise loops exist under `src/trading/ai/` (`loop.py`, `advise.py`);
  no `runtime.py` / `packets.py` yet.
- Read-only local dashboard: `trading dashboard serve|snapshot` (ADESK-A0.1).
- `TokenBudget` persists monthly spend per `(year_month, role)` via
  `agent_budget_ledger` (ADESK-A0.3).
- Agent runs record resolved model id, temperature/seed, and full request/response
  artifacts (ADESK-A0.4).
- `judgment.py` scores setup and agent confidence Brier separately (ADESK-A0.5).
- `evaluation.yaml` `charges_per_lot.verified_at` is `2026-09-19` (published
  schedule estimate; contract-note reconciliation still required for LIVE).
- PAPER positional review + software stops; 60s poll not live-safe.

## Remaining work

Stage 0 (ADESK-A0.1..A0.6) is complete. **C1 resolved (2026-09-20): BOUNDED =
config-promotion only** (no intraday LLM on the live path). Next: fresh
**Stage A planning pass**, then implement ADESK-A1+ one slice per PR.

## Blocking gaps

- **C1 (resolved 2026-09-20):** BOUNDED means config-promotion only. Intraday
  remains “never LLM”. Stage D promotions may grant BOUNDED only for
  config-promotion actions, never for live sizing/stops/submits.
- **C2 (deferred):** invariant 21 is not amended; Layer 4 guarantees replay via
  stored artifacts, not bitwise reproduction.
- **C3 (resolved):** schedule-based `verified_at` is sufficient for paper net
  P&L scoring; LIVE promotion still needs a contract-note cross-check.
- Broker-resident protective orders / sub-60s protection remain the highest
  live-safety gap in the repo.

## Acceptance

- Clean git tree: `uv run mypy` and focused pytest green without relying on
  untracked files.
- Two agent runs share one monthly budget key; third can exhaust role budget
  without freezing L1–L3 trading.
- A recorded agent run stores provider model id, temperature/seed, request,
  tool results, raw response.
- Judgment report exposes agent Brier separately from setup-score Brier.
- CURRENT_STATE Verification matches that evidence; charges story is consistent.

## Non-goals

- Any DeskRole, AuthorityGrant, TradeThesis, terminal policy, or SHADOW desk.
- Touching OMS/broker submit paths or widening gateway approve logic.
- Amending SAFETY_INVARIANTS without an explicit C2 decision.
- Stage A+ implementation.
