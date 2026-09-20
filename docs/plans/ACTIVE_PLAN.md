# Active Implementation Plan

PLAN_STATUS: APPROVED
CONTEXT_DIGEST_VERSION: 11
PLANNED_AT: 2026-09-20
CONTEXT_REFRESH_REQUIRED: no

Milestone: **Agent Desk Stage B — desks in SHADOW (ADESK-B5 DONE; next: B6).**

## Goal

Introduce a single role-based agent runtime and migrate the existing L4
**weekly** and **advise** loops onto it, without changing live-path behaviour.
B1–B2 landed. Continue B3–B10 one slice per commit; SHADOW/ADVISORY only.

Stage A (ADESK-A1..A9) is **closed** on `main` (tip includes A9 packets).

## Binding decisions (do not re-litigate)

- **C1 (2026-09-20):** `AuthorityMode.BOUNDED` = **config-promotion only**.
  Intraday stays L3+L2 with **no LLM**. Unchanged for Stage B.
- Stage B desks are **SHADOW / ADVISORY narration only** until Stage D. B1 does
  not grant BOUNDED live-path actions.
- PART 0 hard rules: one slice per PR; no edits to `src/trading/oms/`,
  `src/trading/broker/`, or decision logic in `src/trading/risk/gateway.py`
  except new reject reasons; contracts Pydantic strict/frozen under
  `src/trading/domain/contracts/`; enums in `enums.py`; no free-text except
  `narrative` and nothing may parse it; refuse PART 17 non-goals.

## Code map (real tree — for B1)

| Spec name | Actual module / entrypoint |
| --- | --- |
| weekly | `src/trading/ai/loop.py` → `run_weekly_agent` → `AIProposal` |
| advise (spec sometimes said “finance”) | `src/trading/ai/advise.py` → `run_advise_agent` → `StructureAdvice` |
| tests | `tests/test_l4_agent.py` (must keep passing unchanged through new runtime) |
| CLI | `trading agent weekly` / `trading agent advise` |

There is **no** `ai/finance.py`. Do not invent one; migrate `advise.py`.

## Build order (one PR per ledger row; do not reorder)

| ID | Slice | Acceptance (summary) |
| --- | --- | --- |
| ADESK-B1 | `ai/runtime.py` role-based runtime; migrate weekly + advise onto it | Existing `tests/test_l4_agent.py` pass without behaviour change; weekly/advise call shared runtime; still default-off / no live influence |
| ADESK-B2 | ENTRY desk + StrikeShortlist + EntryAdvice, SHADOW | Shadow decisions logged; zero live influence |
| ADESK-B3 | POSITION desk SHADOW + delta packet | Slot shadow log alongside deterministic |
| ADESK-B4 | Review-level labelling in judgment | `trading evaluate reviews` metrics |
| ADESK-B5 | Cold-review scheduler + warm_cold_divergence | Every Nth review dual-path |
| ADESK-B6 | PORTFOLIO desk SHADOW | Shared-fate recall metric |
| ADESK-B7 | MACRO desk + MacroCalendar + injection suite | Adversarial headline suite |
| ADESK-B8 | POSTTRADE desk + TradeAttribution | Dual-entry journal hash |
| ADESK-B9 | FRAGILITY desk ADVISORY | Stress narration only |
| ADESK-B10 | agent_scorecard + CLI | PART 14 metrics |

## Progress

- ADESK-B1 DONE (`ai/runtime.py`).
- ADESK-B2 DONE: ENTRY `StrikeShortlist`/`EntryAdvice` SHADOW hook (`ai/entry.py`, `domain/contracts/entry.py`, `tests/test_entry_desk.py`).
- ADESK-B3 DONE: POSITION SHADOW over DeltaPacket (`ai/position.py`).
- ADESK-B4 DONE: review precision/capture + `trading evaluate reviews`.
- ADESK-B5 DONE: cold-review every 5th + warm_cold_divergence.
- Next READY: **ADESK-B6** PORTFOLIO SHADOW.

## ADESK-B1 scope (landed)

**In scope**

1. Add `src/trading/ai/runtime.py`: shared role-scoped runner (prompt assembly,
   tool loop, budget, recording hooks) parameterized by `DeskRole` / authority
   mode resolution (reuse Stage A `authority.py` demotion — missing/expired/
   triple mismatch → OBSERVE).
2. Thin `run_weekly_agent` and `run_advise_agent` to call that runtime; keep
   public signatures and return types (`AIProposal`, `StructureAdvice`).
3. Preserve disable / ABSTAIN / PASS fail-closed paths and CLI flags.
4. Tests: existing L4 agent tests green; add focused runtime unit tests only if
   needed for the new module (no behaviour change to weekly/advise contracts).

**Out of scope (stop and flag if tempted)**

- ENTRY/POSITION/other desk prompts or SHADOW logging (B2+).
- OMS / broker / gateway decision changes.
- Enabling LLM on the intraday path; BOUNDED live-path actions.
- New multi-agent frameworks (PART 17).
- Parsing `narrative`.
- Renaming CLI verbs or breaking `tests/test_l4_agent.py` expectations.

## Nested cadence (unchanged)

1. **Intraday** — `trading paper session`: L3+L2 only. No LLM.
2. **Pre-open day** — Telegram OAuth + readiness.
3. **Week / period** — weekly + advise loops only; still default-off until paper
   evidence.

## Acceptance (Stage B plan done when)

- ADESK-B1..B10 each DONE with green `ruff` / `mypy` / `pytest` as landed.
- Zero new LLM calls on the intraday order path.
- CURRENT_STATE Verification cites Stage B evidence as rows complete.

## Non-goals

- Stage C/D/E code in this plan.
- Amending C1 or “intraday never LLM”.
- Auto-renewal of grants; agents writing grants.
