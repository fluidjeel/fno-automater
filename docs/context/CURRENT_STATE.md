# Current State

LAST_UPDATED: 2026-09-20
CURRENT_MILESTONE: Agent Desk Stage C COMPLETE; Stage D plan APPROVED (ADESK-D1 READY)
STATUS: P0_HARDENED_P1_SELECTION_UNATTENDED_NOT_LIVE_SAFE

## Confirmed decisions

- Deterministic code owns live signals, risk, orders and protection. No LLM on
  the live/intraday path. Weekly AI may propose strategy-family stances only.
- Agent Desk **BOUNDED** (C1, 2026-09-20): config-promotion only.
- Stage C terminal policy (C1–C4) DONE: decided at entry, freeze + one-way
  revert, cost vs flatten; **zero LLM** on that path.
- Stages 0/A/B DONE on `main` (A9 `395b6aa`, B10 `478227b`). Stage D unlocks
  **ADESK-D1 only**.

## Implemented (Agent Desk)

- Stage 0: dashboard, budget ledger, model lineage, agent Brier, paper
  `charges_per_lot.verified_at`.
- Stage A: AuthorityGrant/demotion, DecisionLog, thesis invalidation,
  exposure/stress, bias battery, improvements, reason preconditions, versioned
  packets — tests named in `docs/plans/TASK_LEDGER.md`.
- Stage B: shared desk runtime + SHADOW/ADVISORY desks B1–B10
  (`tests/test_*_desk.py`, `tests/test_ai_runtime.py`).
- Stage C: TerminalPolicy eligibility, ExitPolicy freeze, continuous
  enforcement + one-way revert, scorecard cost
  (`tests/test_terminal_policy*.py`, `tests/test_terminal_enforcement.py`,
  `tests/test_terminal_policy_cost.py`).
- L4 weekly + advise remain default-off (`ai/loop.py`, `ai/advise.py`).

## Verification

- Ledger: ADESK-A0.* through ADESK-C4 DONE; next READY is ADESK-D1.
- Tip includes Stage D plan approval `6b011a9` (C4 code `88074e5`).

## Blocking gaps

- 60s software-only poll cannot see intra-interval stop prints — not live-safe.
- LIVE market-rule values in base config remain unverified.
- CAS depth still needs a live 15:00–15:30 IST window for promotion evidence.

## Next action

Implement **ADESK-D1** only: `ConfidenceBucket` + Phase-1 downscale-only sizing
(`size_multiplier <= 1` by type). D2+ stay BLOCKED. Do not treat software stops
or the 60s poll as live-safe.

## Update rules

- Keep this file below 120 lines.
- Record facts with file/test evidence; do not paste logs or plans.
- Update only after a meaningful milestone or blocker change.
