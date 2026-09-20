# Active Implementation Plan

PLAN_STATUS: APPROVED
CONTEXT_DIGEST_VERSION: 13
PLANNED_AT: 2026-09-20
CONTEXT_REFRESH_REQUIRED: no

Milestone: **Agent Desk Stage E — research loop (E1 READY).** Stages A–D COMPLETE.

## Goal

Close the learning loop: weekly RESEARCH clusters improvements + bias battery,
promotes eligible notes to hypotheses on the **existing** SHADOW ladder (no new
gate), and a monthly meta-report. Advisory / research only — C1 unchanged.

## Binding decisions (do not re-litigate)

- **C1 (2026-09-20):** `AuthorityMode.BOUNDED` = config-promotion only. Intraday
  L3+L2, **no LLM**. Stage E does not grant live-path BOUNDED.
- RESEARCH is weekly / ADVISORY. Hypotheses never auto-implement.
- Reuse `analytics/improvements.py` and `analytics/bias.py` — do not reinvent.
- Hard rules: `.cursor/rules/00-core.mdc` and PART 0 of
  `docs/plans/AGENT_DESK_SPEC.md`. Do not restate them here.
- **Terminal rule:** run-to-expiry is decided **at entry** (Stage C; zero LLM).

## Code map (Stage E)

| Concern | Where |
| --- | --- |
| ImprovementRecord / clusters | `domain/contracts/improvement.py`, `analytics/improvements.py` |
| Bias battery | `analytics/bias.py` |
| Desk scorecard | `analytics/agent_scorecard.py` |
| RESEARCH role | `DeskRole.RESEARCH`; agent runs under `data/agent_runs/` |

## Progress / next

- Stages A–D DONE on `main` (D6 `2484fe2`). Evidence: `TASK_LEDGER.md` — do not
  reload finished-slice recipes.
- Stage E APPROVED; **only ADESK-E1 READY**. E2–E3 stay BLOCKED until prior DONE.

**ADESK-E1 (READY):** weekly RESEARCH builder — cluster improvements, bias
summary, structured playbook-edit proposals; persist under `data/agent_runs/`;
focused tests. Out of scope: auto-implement edits, new promotion ladder (E2),
monthly meta-report (E3), OMS/broker/live BOUNDED.

- Stages A–D complete on `main` (tip includes D6 `2484fe2`).
- **Stage E complete (E1–E3 DONE).** Research loop advisory-only under C1.

## Acceptance

E1–E3 DONE with focused tests + ledger/CURRENT_STATE; zero live-path LLM;
hypotheses only via existing SHADOW gate.
