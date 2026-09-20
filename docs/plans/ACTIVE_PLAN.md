# Active Implementation Plan

PLAN_STATUS: APPROVED
CONTEXT_DIGEST_VERSION: 11
PLANNED_AT: 2026-09-20
CONTEXT_REFRESH_REQUIRED: no

Milestone: **Agent Desk Stage C — terminal policy (C1–C4).**

## Goal

Land PART 5 terminal policy with **zero LLM**: eligibility at entry, freeze into
`ExitPolicy`, continuous deterministic enforcement with one-way revert, and cost
accounting in the scorecard. ENTRY may remain SHADOW — an agent request is
logged/scored; the deterministic default applies when ineligible or disabled.

## Binding decisions (do not re-litigate)

- **Authority BOUNDING (2026-09-20):** `AuthorityMode.BOUNDED` = **config-promotion
  only**. This is **not** ADESK-C1. Intraday stays L3+L2 with **no LLM**.
- **Terminal rule:** run-to-expiry is decided **at entry**, never at T-1.
- ADESK-C1..C3 contain **no LLM call**. Agent request at entry is optional/SHADOW;
  deterministic gate + enforcement own the path.
- PART 0 hard rules: one slice per PR; no edits to `src/trading/oms/`,
  `src/trading/broker/`, or decision logic in `src/trading/risk/gateway.py`
  except new reject reasons; contracts Pydantic strict/frozen under
  `src/trading/domain/contracts/`; enums in `enums.py`; no free-text except
  `narrative` and nothing may parse it; refuse PART 17 non-goals.

## Code map (real tree)

| Spec name | Actual module / notes |
| --- | --- |
| ExitPolicy | `src/trading/domain/contracts/position.py` |
| InvalidationCondition | `src/trading/domain/contracts/trade_thesis.py` |
| defined-risk helper | `src/trading/risk/gateway.py` (`_is_defined_risk` — reuse only) |
| EventRiskState | `src/trading/news/contracts.py` |
| Money | `src/trading/domain/primitives.py` |
| ENTRY SHADOW | `src/trading/ai/entry.py` |
| scorecard | `src/trading/analytics/agent_scorecard.py` |
| review / exits | `src/trading/trade/review.py`, `src/trading/trade/exits.py` |

## Build order (one PR per ledger row; do not reorder)

| ID | Slice | Acceptance (summary) |
| --- | --- | --- |
| ADESK-C1 | `TerminalPolicy` contracts + eligibility gate (PART 5.2) | All seven conditions individually tested; ineligible → `FLATTEN_AT_DTE` |
| ADESK-C2 | Freeze terminal policy into `ExitPolicy` at entry; restore on restart | Restart test preserves policy + `run_conditions` |
| ADESK-C3 | Deterministic continuous enforcement + one-way revert | Broken condition reverts; cannot be re-granted |
| ADESK-C4 | Terminal-policy cost accounting in scorecard | Report shows cost saved vs counterfactual flatten, in R and INR |

## Progress

- Stage A / B complete on `main` (tip includes B10 `478227b`).
- ADESK-C1..C2 DONE. **ADESK-C3 READY**. C4 BLOCKED until C3 DONE.

## ADESK-C1 (DONE)

**In scope**

1. Enums: `TerminalPolicyKind` (`FLATTEN_AT_DTE`, `RUN_TO_EXPIRY_DEFINED_RISK`,
   `FLATTEN_EARLY_IF_FRAGILE`) in `enums.py`.
2. Contracts in e.g. `domain/contracts/terminal_policy.py`:
   `TerminalPolicyRequest`, accepted/frozen policy snapshot, eligibility input
   DTO, closed reject-reason enum.
3. Pure eligibility gate for PART 5.2 seven conditions; on any miss accept
   `FLATTEN_AT_DTE` with configured default DTE.
4. Matrix tests: each condition fails alone → fallback; all pass →
   `RUN_TO_EXPIRY_DEFINED_RISK` accepted.

**Out of scope (stop and flag)**

- Mutating OMS/broker; changing gateway *decision* logic (read/reuse only).
- Wiring freeze into ExitPolicy persistence (C2).
- Continuous enforcement / revert (C3).
- Scorecard cost lines (C4).
- LLM calls; BOUNDED live-path ENTRY actions.
- Parsing `narrative`.

## Nested cadence (unchanged)

1. **Intraday** — `trading paper session`: L3+L2 only. No LLM.
2. Terminal enforcement is deterministic on review/protection polls (C3).

## Acceptance (Stage C plan done when)

- ADESK-C1..C4 each DONE with green focused tests + ledger/CURRENT_STATE.
- Zero LLM on terminal-policy path.
