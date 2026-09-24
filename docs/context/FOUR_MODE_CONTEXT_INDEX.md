# Four-mode context index

**Updated:** 24 September 2026  
**HEAD inspected:** working branch (dirty ops tree; do not reset)  
**Spec:** `docs/plans/NIFTY_FOUR_MODE_CURSOR_REDESIGN.md` v1.1  
**Plan:** `docs/plans/FOUR_MODE_REDESIGN_PLAN.md`  
**Status table:** `docs/plans/REQUIREMENT_TRACEABILITY.md`

## Product in one paragraph

Four NIFTY option modes share one Layer 2 arbiter. M1 is microstructure long options. M2 is single-leg directional with a following-week expiry. M3 is vertical spreads. M4 is the longer basket, including defined-risk condors, butterflies, and long volatility structures. **Calendars stay experimental** (`EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN`) and off the strict ₹7L book. AI proposes. Deterministic code authorizes. PAPER only.

## Decisions

- Equity ₹7,00,000. Shares 10/20/30/40 from `config/modes.yaml`.
- No cross-mode borrow. G1 failure ⇒ `MIN_LOT_EXCEEDS_BUDGET`.
- G2: class or payoff test ≠ PAPER. Need entry, partial fill, monitor, exit, restart.
- G3 before any M1 work. 60s poll is not microstructure entry.
- Calendars: research registry + settlement ledger; refuse same-expiry payoff formulas (P14).
- Activity funnel and operator G1/G2 view on cycle evidence (P15).

## Where behavior lives

| Concern | Path |
| --- | --- |
| Calendar research registry | `src/trading/research/registry.py` |
| Dual-expiry settlement ledger | `src/trading/risk/calendar_settlement.py` |
| Calendar binders | `src/trading/identification/binders.py` (`bind_long_call_calendar`, `bind_long_put_calendar`) |
| Activity funnel | `src/trading/runtime/activity_funnel.py` |
| Operator family view | `src/trading/runtime/family_status.py` |
| Cycle evidence | `src/trading/runtime/cycle_evidence.py`, `src/trading/domain/contracts/cycle_evidence.py` |
| Dashboard four-mode panel | `src/trading/dashboard/static/app.js`, `collector.py` |
| CLI operator view | `trading evaluate operator-view` |

## Current phase

**P16 complete.** All redesign implementation phases P1–P16 are on the working branch. Calendars remain experimental. LIVE blocked.

## Known limitations

- 60-second software poll can miss an intra-interval stop print.
- Calendars: no finite loss bound proven across two settlements.
- Straddle/strangle G1-blocked on ₹7L cap.
- M1 PAPER on 60s loop BLOCKED (G3).
