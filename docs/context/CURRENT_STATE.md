# Current State

LAST_UPDATED: 2026-09-20
CURRENT_MILESTONE: Agent Desk Stage B — SHADOW desks (ADESK-B1 READY)
STATUS: P0_HARDENED_P1_SELECTION_UNATTENDED_NOT_LIVE_SAFE

## Confirmed decisions

- Deterministic code owns live signals, risk, orders and protection.
- Weekly AI may propose strategy-family stances only; never sizes, stops, or submits.
- No LLM on the live/intraday path.
- Agent Desk **BOUNDED** (C1, 2026-09-20): config-promotion only.
- Stage A (ADESK-A1..A9) closed on `main` (A9 packets `395b6aa` lineage).
- Stage B plan APPROVED (`docs/plans/ACTIVE_PLAN.md` digest v11): next code is
  **ADESK-B1** (`ai/runtime.py`; migrate `run_weekly_agent` + `run_advise_agent`).

## Implemented (Agent Desk)

- Stage 0 measurement: dashboard, monthly budget ledger, temp=0/seed/resolved
  model, separate setup vs agent Brier, paper charges `verified_at`.
- A1–A2: `AuthorityGrant` / demotion; `AgentDecision` / `DecisionLog`
  (`tests/test_authority_grant.py`, `tests/test_agent_decision.py`).
- A3–A5: thesis invalidation; `ExposureReport` + risk limits; `StressReport` +
  `assume_no_fills` / entry freeze (`tests/test_*` under those modules).
- A6–A9: bias battery (11 metrics); `ImprovementRecord` +
  `trading evaluate improvements`; reason preconditions → ABSTAIN +
  `hallucination_events`; versioned delta packets + `delta_gap_rate`
  (`tests/test_bias_battery.py`, `tests/test_improvement_records.py`,
  `tests/test_reason_preconditions.py`, `tests/test_packets.py`).
- L4 weekly + advise loops still live in `ai/loop.py` / `ai/advise.py`
  (`tests/test_l4_agent.py`); default-off.

## Verification

- Stage A rows ADESK-A1..A9 marked DONE in `docs/plans/TASK_LEDGER.md`.
- Focused suites above plus `tests/test_l4_agent.py` are the regression gate
  for B1 (must stay green through the runtime migration).

## Blocking gaps

- 60s software-only poll cannot see intra-interval stop prints — not live-safe.
- LIVE market-rule values in base config remain unverified.
- CAS depth still needs a live 15:00–15:30 IST window for promotion evidence.
- ADESK-B1 not yet implemented (planning pass only as of this update).

## Next action

Implement **ADESK-B1** only: shared `ai/runtime.py`, thin weekly/advise wrappers,
no OMS/broker/gateway decision edits, no new desks (B2+). Monday ops: Fyers auth,
CAS depth benchmark, supervised paper — outside this slice.

## Update rules

- Keep this file below 120 lines.
- Record facts with file/test evidence; do not paste logs or plans.
- Update only after a meaningful milestone or blocker change.
