# Active Implementation Plan

PLAN_STATUS: APPROVED
CONTEXT_DIGEST_VERSION: 13
PLANNED_AT: 2026-09-20
CONTEXT_REFRESH_REQUIRED: no

Milestone: **Agent Desk Stage D — authority ladder (D1 READY).** Stage C (C1–C4) COMPLETE.

## Goal

Climb authority one rung at a time under C1: Phase-1 downscale-only sizing first;
live-path desk actions stay SHADOW/ADVISORY; BOUNDED is config-promotion only.

## Binding decisions (do not re-litigate)

- **C1 (2026-09-20):** `AuthorityMode.BOUNDED` = **config-promotion only**.
  Intraday stays L3+L2 with **no LLM**. Spec Stage D rows that grant live-path
  BOUNDED are **re-scoped by the ledger**.
- **Terminal rule:** run-to-expiry is decided **at entry**, never at T-1.
  Stage C owns that path with **zero LLM**.
- Hard rules: `.cursor/rules/00-core.mdc` and PART 0 of
  `docs/plans/AGENT_DESK_SPEC.md`. Do not restate them here.

## Code map (Stage D)

| Concern | Where |
| --- | --- |
| AuthorityGrant / `BOUNDED_ACTIONS` | `domain/contracts/authority.py`, `ai/authority.py`, `domain/enums.py` |
| ENTRY / POSITION / PORTFOLIO / FRAGILITY / POSTTRADE | `ai/entry.py`, `position.py`, `portfolio_desk.py`, `fragility.py`, `posttrade.py` |
| `size_multiplier` | already `le=1` on some advice contracts — D1 adds `ConfidenceBucket` + Phase-1 |

## Progress / next

- Stages A/B/C DONE on `main` (C4 `88074e5`). C1–C4 evidence:
  `docs/plans/TASK_LEDGER.md` — do not reload finished-slice recipes.
- Stage D APPROVED; **only ADESK-D1 READY**. D2–D6 stay BLOCKED until prior DONE.

**ADESK-D1 (READY):** `ConfidenceBucket` + Phase-1 map to `size_multiplier` with
type-level `<= 1.0`; wire into ENTRY/shared sizing advice without live BOUNDED.
Tests: each bucket; reject `>1`; unknown fail-closed. Out of scope: D2–D6,
OMS/broker, LLM on the intraday path.

## Acceptance

Ledger D1–D6 DONE with focused tests; zero live-path BOUNDED; C1 honored.
