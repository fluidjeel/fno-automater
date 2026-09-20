# Active Implementation Plan

PLAN_STATUS: APPROVED
CONTEXT_DIGEST_VERSION: 11
PLANNED_AT: 2026-09-20
CONTEXT_REFRESH_REQUIRED: no

Milestone: **Agent Desk Stage D — authority ladder (D1–D6).**

## Goal

Climb authority one rung at a time under the binding **C1** decision:
`AuthorityMode.BOUNDED` = **config-promotion only**. No LLM on the live order
path. Live-path desk actions stay SHADOW/ADVISORY; BOUNDED is only for closed
config-promotion proposals.

## Binding decisions (do not re-litigate)

- **C1 (2026-09-20):** `AuthorityMode.BOUNDED` = config-promotion only. Intraday
  stays L3+L2 with **no LLM**. Spec Stage D table rows that grant live-path
  BOUNDED (raw D3–D5 in AGENT_DESK_SPEC) are **re-scoped by the ledger**.
- PART 0 hard rules: one slice per PR; no OMS/broker edits; no gateway decision
  logic except new reject reasons; contracts strict/frozen; enums in `enums.py`;
  no free-text except `narrative` (nothing parses it); refuse PART 17.
- Prefer type-enforced ceilings (`size_multiplier <= 1` via Field) over runtime
  checks when the acceptance text says "by type, not by check".

## Code map

| Spec / ledger | Actual |
| --- | --- |
| AuthorityGrant / demotion | `domain/contracts/authority.py`, `ai/authority.py` |
| AgentAction closed sets | `domain/enums.py` (`BOUNDED_ACTIONS`, `ADVISORY_ACTIONS`) |
| ENTRY / POSITION / PORTFOLIO / FRAGILITY / POSTTRADE | `ai/entry.py`, `position.py`, `portfolio_desk.py`, `fragility.py`, `posttrade.py` |
| size_multiplier | already `le=1` on some advice contracts — D1 formalizes ConfidenceBucket + Phase-1 |

## Build order (ledger — C1-safe; do not reorder)

| ID | Slice | Acceptance |
| --- | --- | --- |
| ADESK-D1 | ConfidenceBucket enum + Phase-1 downscale-only sizing | `size_multiplier <= 1.0` enforced by type; bucket→multiplier table; tests |
| ADESK-D2 | Promote FRAGILITY + POSTTRADE to ADVISORY | Signed AuthorityGrant; Telegram/advisory payload path; tests |
| ADESK-D3 | PORTFOLIO BOUNDED for **config-promotion proposals only** (C1) | Veto/approve-as-order paths absent; only PROPOSE_* config actions BOUNDED |
| ADESK-D4 | Re-scope POSITION: SHADOW/ADVISORY only for tighten/partial (**not** BOUNDED) | Mode/grant tests; live BOUNDED for TIGHTEN/PARTIAL rejected |
| ADESK-D5 | Re-scope ENTRY: SHADOW/ADVISORY for veto/reduce; BOUNDED only if config-promotion | Same pattern as D4/D3 |
| ADESK-D6 | Phase-2 upscale unlock (**deterministic envelope only**; never agent BOUNDED) | Upscale only via deterministic calibration gates; agent path cannot emit >1 |

## Progress

- Stage A/B/C complete on `main` (tip includes C4 `88074e5`).
- Stage D planning APPROVED; **only ADESK-D1 READY**. D2+ BLOCKED until prior DONE.

## ADESK-D1 scope (only READY code slice)

**In scope**

1. `ConfidenceBucket` enum (closed set) in `enums.py`.
2. Deterministic Phase-1 map bucket → `size_multiplier` with **type-level**
   `<= 1.0` (Pydantic Field / Annotated); reject >1 at validation.
3. Wire into ENTRY (and any shared sizing advice) without enabling live BOUNDED.
4. Tests: each bucket; reject multiplier >1; default/unknown fail-closed.

**Out of scope**

- Live-path BOUNDED for ENTRY/POSITION/PORTFOLIO veto/tighten.
- Phase-2 upscale (D6).
- OMS/broker/gateway decision changes.
- LLM on intraday path.

## Acceptance (Stage D plan done when)

- ADESK-D1..D6 each DONE with green focused tests + ledger/CURRENT_STATE.
- Zero live-path BOUNDED actions; C1 honored.
