# Antigravity Layer Execution Prompt — NIFTY Four-Mode Redesign

This document records the master orchestration prompt and per-phase tails for executing the four-mode paper-trading redesign for repository `/Users/apple/Documents/manasjit/fno-automated`.

---

## Authority & Canonical References

1. **Canonical spec:** `docs/plans/NIFTY_FOUR_MODE_CURSOR_REDESIGN.md` v1.1
2. **Execution order:** `docs/plans/FOUR_MODE_REDESIGN_PLAN.md`
3. **Per-component verdicts:** `docs/plans/FOUR_MODE_COMPONENT_CHANGES.md` (KEEP / EXTEND / OVERHAUL / NEW / DISABLE)
4. **Per-component required changes + success criteria:** `docs/plans/FOUR_MODE_LAYER_CHANGE_CRITERIA.md`
5. **Requirement status:** `docs/plans/REQUIREMENT_TRACEABILITY.md`
6. **Resume sheet:** `docs/context/FOUR_MODE_CONTEXT_INDEX.md`
7. **Core rules:** `.cursor/rules/00-core.mdc`

**Never implement from `docs/research/`** (superseded history).

---

## Core Invariants

- Paper equity: **₹7,00,000**. Four mode shares 10/20/30/40 are a seed until G1 rows exist per family.
- **No cross-mode borrow.** Reference capital = min(start-of-session allocation, conservative equity).
- **G2 ladder:** `IMPLEMENTED_UNIT` → `LIFECYCLE_PROVEN` → `PAPER_STANCE_ENABLED`. Payoff/unit tests do NOT flip stance.
- **Legacy PAPER** in `config/paper_session.yaml` is today's config, not redesigned-policy certification.
- **Open positions** keep stored owner, exit policy, and snapshot rules — do not rewrite on migration.
- **Commodity** stays SHADOW; gateway must reject non-NIFTY execution underlyings (P1).
- **Do not change 15:30 / 15:40 clocks** until an effective circular + broker check is recorded.
- **Calendars** stay `EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN` until dual-expiry bound proven (P14).
- **Do not register `directional_conviction`** as a live strategy.
- **Distinct leg snapshot IDs** — never copy parent id onto every leg (`snapshot_bundle.py` rule).
- **Zero LLM on the order path** — Agent Desk C1.
- **Dirty tree:** do NOT git reset. Preserve ops/paper-autopilot changes in `deploy/`, `src/trading/ops/`, dashboard, etc.
- **LIVE trading stays blocked.** No `LIVE_APPROVED`. Agent Desk Stage E stays historical DONE — do not reopen.

---

## Global Phase Sequence

| Phase | Scope | Status |
| --- | --- | --- |
| P0 | Review docs only | ✅ Completed |
| G1 | One-lot affordability report | ✅ Completed (`docs/reports/G1_ONE_LOT_AFFORDABILITY.md`) |
| G3 | CAS field + latency measurement | ✅ Completed (`docs/reports/G3_CAS_FIELD_AND_LATENCY_REPORT.md`) |
| **P1** | `ModeId` / `ModePolicy`, `config/modes.yaml`, startup validation (G1/G2/G3 cross-checks), NIFTY execution reject | ✅ Completed |
| **P2** | Four ledgers, no borrow, restart-safe reservations | ✅ Completed |
| **P3** | Same-expiry payoff check for four verticals | ✅ Completed |
| **P4** | M2 long + M3 debit producers, exact-duplicate arbiter | ✅ Completed |
| **P5** | M2 following-week expiry, 0/1 DTE exclusion | ✅ Completed |
| **P6** | M3 credit verticals, long-then-short, G2 | ✅ Completed |
| **P7** | M1 selector on G3 path (only if not BLOCKED on poll) | ✅ Completed |
| **P8** | Whole-structure spread valuation | ✅ Completed |
| **P9** | M2 carry gate | ✅ Completed |
| P10 | Iron condor binder, prefix safety, G2 | ✅ Completed |
| P11 | Butterfly, straddle, strangle, iron butterfly (one family at a time) | ✅ Completed |
| P12 | Economic overlap, conflict policy, M4 cap 2 | ✅ Completed |
| P13 | Missed-review recovery; ROLL/SWITCH only where G2 close/open exists | ✅ Completed |
| P14 | Calendar research registry | ✅ Completed |
| P15 | Activity funnel + four-mode operator view | ✅ Completed |
| P16 | Doc reconciliation + remaining scenario tests | ✅ Completed |

---

## Follow-on Tails (P2 through P16)

### Phase P2: Four Capital Ledgers & Reservations
```markdown
## THIS SESSION
**Start at phase:** P2
**Layers:** 2.2, 2.3, 2.8, 2.17, 2.18 (+ 1.3 if master lot reads block G1)
Read FOUR_MODE_LAYER_CHANGE_CRITERIA.md §2.2–2.3, §2.8, §2.17–2.18.
Exit: four ledgers on ₹7L, restart restores reservations, Mode A cannot spend Mode B cash, G1 failures reserve nothing.
```

### Phase P3: Vertical Spread Payoff Checkers
```markdown
## THIS SESSION
**Start at phase:** P3
**Layers:** 2.4, 2.6, 3.7, 3.8 — payoff checker for four verticals
Read FOUR_MODE_LAYER_CHANGE_CRITERIA.md §2.4, §2.6, §3.7, §3.8.
Exit: formula and generic kink/slope payoff checker agree; status IMPLEMENTED_UNIT for four verticals.
```

### Phase P4: M2 Long & M3 Debit Producers + Exact-Duplicate Arbiter
```markdown
## THIS SESSION
**Start at phase:** P4
**Layers:** 3.2, 3.3, 3.6, 3.7, 2.7, 2.19, 4.3 — mode producers, exact-duplicate arbiter
Read FOUR_MODE_LAYER_CHANGE_CRITERIA.md §3.2, §3.3, §3.6, §3.7, §2.7, §2.19, §4.3.
Exit: one executable portfolio; counterfactual log does not reserve; exact duplicates suppressed.
```

### Phase P5: M2 Following-Week Expiry & Calendar Filter
```markdown
## THIS SESSION
**Start at phase:** P5
**Layers:** 1.10, 3.3 — following-week expiry selection, 0/1 DTE ban
Read FOUR_MODE_LAYER_CHANGE_CRITERIA.md §1.10, §3.3, §3.6.
Exit: M2 selects following listed week contract from master; 0/1 DTE excluded; abstain with reason.
```

### Phase P6: M3 Credit Verticals & Prefix Safety
```markdown
## THIS SESSION
**Start at phase:** P6
**Layers:** 3.8, 2.1 — credit verticals split, long-then-short approval, full G2
Read FOUR_MODE_LAYER_CHANGE_CRITERIA.md §3.8, §2.1.
Exit: bull put and bear call credit separated; long protection submitted before short; G2 lifecycle proven.
```

### Phase P7: M1 CAS Selector on Event-Driven Path
```markdown
## THIS SESSION
**Start at phase:** P7
**Layers:** 3.5, 1.2, 1.4 — M1 strike selector, episode id, quote/depth cohorts
Read FOUR_MODE_LAYER_CHANGE_CRITERIA.md §3.5, §1.2, §1.4.
Exit: M1 selector operates in research delta band [0.15, 0.35]; 0-DTE default off; wire EXP-M1-QUOTE/DEPTH.
```

### Phase P8: Whole-Structure Spread Valuation
```markdown
## THIS SESSION
**Start at phase:** P8
**Layers:** 2.12, 2.13 — whole-structure spread valuation, position lifecycle
Read FOUR_MODE_LAYER_CHANGE_CRITERIA.md §2.12, §2.13.
Exit: spreads mark and stop on conservative whole-structure value; legacy open trades keep entry stops.
```

### Phase P9: M2 Carry Gate
```markdown
## THIS SESSION
**Start at phase:** P9
**Layers:** 2.15 — thesis-based carry gate replacing EOD profit scanner
Read FOUR_MODE_LAYER_CHANGE_CRITERIA.md §2.15.
Exit: CARRY_APPROVED and CARRY_REJECTED based on thesis, remaining expiry, overnight budget, event check.
```

### Phase P10: Iron Condor Binder, Prefix Safety, G2
```markdown
## THIS SESSION
**Start at phase:** P10
**Layers:** 3.9, 2.1, 2.4 — iron condor binder, prefix safety, G2 lifecycle
Read FOUR_MODE_LAYER_CHANGE_CRITERIA.md §3.9, §2.1, §2.4.
Exit: asymmetric wing loss formula; long legs submitted before short legs; G2 lifecycle proven.
```

### Phase P11: M4 Broad Basket (Butterflies, Straddle, Strangle)
```markdown
## THIS SESSION
**Start at phase:** P11
**Layers:** 3.10 — butterflies (1:2:1), long straddle, long strangle per family
Read FOUR_MODE_LAYER_CHANGE_CRITERIA.md §3.10.
Exit: one family at a time with own G2 tests; straddle/strangle non-executable while exceeding budget.
```

### Phase P12: Economic Overlap, Conflict Policy, M4 Cap
```markdown
## THIS SESSION
**Start at phase:** P12
**Layers:** 2.7, 4.3, 4.9 — economic overlap, conflict, M4 cap of 2, campaign drawdown
Read FOUR_MODE_LAYER_CHANGE_CRITERIA.md §2.7, §4.3, §4.9.
Exit: bull call and bull put overlap detected; max 2 M4 positions checked; counterfactual book isolated.
```

### Phase P13: Positional Reviews & Missed Slot Recovery
```markdown
## THIS SESSION
**Start at phase:** P13
**Layers:** 2.14 — 10:30 and 14:30 review schedule, missed slot recovery
Read FOUR_MODE_LAYER_CHANGE_CRITERIA.md §2.14.
Exit: single current recovery for missed slots; ROLL/SWITCH submit only where G2 open/close exists.
```

### Phase P14: Calendar Research Registry
```markdown
## THIS SESSION
**Start at phase:** P14
**Layers:** 3.11 — dual-expiry settlement ledger, research registry
Read FOUR_MODE_LAYER_CHANGE_CRITERIA.md §3.11.
Exit: status EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN; same-expiry loss formula refused; off strict book.
```

### Phase P15: Activity Funnel & Four-Mode Operator View
```markdown
## THIS SESSION
**Start at phase:** P15
**Layers:** 2.20, 4.1, 4.8 — activity funnel, cohorts, four-mode operator view
Read FOUR_MODE_LAYER_CHANGE_CRITERIA.md §2.20, §4.1, §4.8.
Exit: complete funnel tracking evaluation to exit; CLI/dashboard shows four mode ledgers and G1/G2 status.
```

### Phase P16: Doc Reconciliation & Scenario Matrix
```markdown
## THIS SESSION
**Start at phase:** P16
**Layers:** 5.3, full test matrix (T01–T58)
Read FOUR_MODE_LAYER_CHANGE_CRITERIA.md §5.3.
Exit: ARCHITECTURE.md, CURRENT_STATE.md, and specs reconciled; honest limitation list; LIVE blocked.
```
