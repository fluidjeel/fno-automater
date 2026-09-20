# Active Implementation Plan

PLAN_STATUS: APPROVED
CONTEXT_DIGEST_VERSION: 11
PLANNED_AT: 2026-09-20
CONTEXT_REFRESH_REQUIRED: no

Milestone: **Agent Desk Stage E — research loop (E1–E3).**

## Goal

Close the learning loop: weekly RESEARCH clusters improvements + bias battery,
promotes eligible notes to hypotheses that enter the **existing** SHADOW
promotion ladder (no new gate), and a monthly meta-report an operator can read
in ~10 minutes. Advisory / research only — C1 unchanged (BOUNDED = config-
promotion only; no live-path LLM).

## Binding decisions (do not re-litigate)

- **C1 (2026-09-20):** `AuthorityMode.BOUNDED` = config-promotion only. Intraday
  L3+L2, **no LLM**. Stage E does not grant live-path BOUNDED.
- RESEARCH is weekly / ADVISORY (PART 3). Hypotheses never auto-implement.
- Reuse `analytics/improvements.py` (`cluster_improvements`, `hypothesis_eligible`)
  and `analytics/bias.py` — do not reinvent.
- PART 0 hard rules: one slice per PR; no OMS/broker/gateway decision edits
  (except new reject reason enums); contracts strict/frozen; enums in `enums.py`;
  no free-text except `narrative`; refuse PART 17.

## Code map

| Spec | Actual |
| --- | --- |
| ImprovementRecord / clusters | `domain/contracts/improvement.py`, `analytics/improvements.py` |
| Bias battery | `analytics/bias.py` (A6) |
| Desk scorecard | `analytics/agent_scorecard.py` (B10) |
| Promotion / SHADOW experiments | `domain/contracts/evaluation.py` ExecutionMode.SHADOW |
| Agent runs dir | `data/agent_runs/` (create under repo or configurable path) |
| RESEARCH role | `DeskRole.RESEARCH` already in enums; `packets.py` hint exists |

## Build order

| ID | Slice | Acceptance |
| --- | --- | --- |
| ADESK-E1 | RESEARCH weekly runner: cluster improvements, run bias battery, propose playbook edits | Writes weekly artifact under `data/agent_runs/` with ranked clusters; tests |
| ADESK-E2 | Hypothesis → experiment contract into existing promotion ladder | Eligible cluster → hypothesis → SHADOW experiment via **existing** gate; tests |
| ADESK-E3 | Monthly meta-report | Desk scorecards + demotions + det-vs-desk comparison; CLI/doc artifact; tests |

## Progress

- Stages A–D complete on `main` (tip includes D6 `2484fe2`).
- Stage E planning APPROVED; **only ADESK-E1 READY**. E2+ BLOCKED until prior DONE.

## ADESK-E1 scope (only READY code slice)

**In scope**

1. `ai/research.py` (or `analytics/research_weekly.py`): pure/deterministic weekly
   builder — input ImprovementRecords (+ optional bias inputs) → ranked clusters,
   bias summary, closed playbook-edit proposals (enums / structured, not free
   prose except narrative).
2. Persist weekly artifact JSON under `data/agent_runs/` (or path from config).
3. CLI hook if natural (`trading evaluate research-weekly` or similar).
4. Tests with fixtures; no LLM required for the builder (LLM optional later).

**Out of scope**

- Auto-implementing playbook edits.
- New promotion ladder (E2).
- Monthly meta-report (E3).
- OMS/broker/live BOUNDED.

## Acceptance (Stage E plan done when)

- E1–E3 DONE with green focused tests + ledger/CURRENT_STATE.
- Zero live-path LLM; hypotheses only via existing SHADOW gate.
