# Current State

LAST_UPDATED: 2026-09-20
CURRENT_MILESTONE: Agent Desk Stage E — research loop (E1 DONE; E2 READY)
STATUS: P0_HARDENED_P1_SELECTION_UNATTENDED_NOT_LIVE_SAFE

## Confirmed decisions

- Deterministic code owns live signals, risk, orders and protection. No LLM on
  the live/intraday path.
- Agent Desk **BOUNDED** (C1, 2026-09-20): config-promotion only.
- Stages 0/A/B/C/D DONE on `main`. Stage C terminal path has zero LLM.
- Stage D under C1: live-path desks SHADOW/ADVISORY; Phase-2 upscale is
  deterministic envelope only (`size_multiplier` le=1 from agents).
- Stage E plan APPROVED (2026-09-20); research/advisory only under C1.
- **ADESK-E1 DONE**: RESEARCH weekly runner, playbook proposals, bias summary,
  JSON under `data/agent_runs/`, CLI `evaluate research-weekly`.

## Implemented (Agent Desk)

- Stages 0–C: see `docs/plans/TASK_LEDGER.md` (A0–C4 DONE).
- Stage D: D1 ConfidenceBucket + Phase-1 map; D2 FRAGILITY/POSTTRADE ADVISORY;
  D3 PORTFOLIO BOUNDED config-promotion; D4–D5 POSITION/ENTRY SHADOW/ADVISORY;
  D6 Phase-2 deterministic upscale envelope.
- L4 weekly + advise remain default-off.

## Verification

- Ledger: ADESK-D1..D6 DONE; next READY is ADESK-E1.
- Tip includes Stage E plan approval `6f23b2a` (D6 `2484fe2`).

## Blocking gaps

- 60s software-only poll cannot see intra-interval stop prints — not live-safe.
- LIVE market-rule values in base config remain unverified.
- CAS depth still needs a live 15:00–15:30 IST window for promotion evidence.

## Next action

Next: **ADESK-E2** — Hypothesis → ExperimentProposal into existing SHADOW
promotion ladder. E3 stays BLOCKED until E2 DONE.

## Update rules

- Keep this file below 120 lines.
- Record facts with file/test evidence; do not paste logs or plans.
- Update only after a meaningful milestone or blocker change.
