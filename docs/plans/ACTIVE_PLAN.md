# Active Implementation Plan

PLAN_STATUS: APPROVED
CONTEXT_DIGEST_VERSION: 15
PLANNED_AT: 2026-09-26
CONTEXT_REFRESH_REQUIRED: no

Milestone: **PAPER Discovery Mode: take trades, record reasoning, find the edge.**

## Goal

Make PAPER take trades in all four modes from Monday 2026-09-28. It must
record every decision with its reasoning and keep hard safety and exits. It is
temporary; the target remains the `STRICT` profile.

## Scope

- Stories: `docs/plans/DISCOVERY_MODE_STORIES.md`. Sprint A (DISC-A0..A12)
  must be done before Monday 09:15 IST.
- Rules: `docs/context/DISCOVERY_MODE.md`.

## Owner decisions (2026-09-26)

- Four independent books of ₹7L each (one per mode). They compound daily
  from realised net P&L.
- Per-trade guide: M1 1%, M2 2%, M3 2%, M4 3% of mode equity. The guide is
  flexible: minimum 1 lot, and strike choice and fill come before sizing.
- Open-risk cap: 12% of each mode's equity. Bug guard: 10% per trade.
- No daily loss stop. A drawdown alert only, at 20%.
- Straddle and strangle move to PAPER. Calendars stay SUSPENDED.
- M1 may fire from the 60-second poll, tagged.
- Paper fills at the displayed ask or bid plus charges. The strict verdict is
  recorded.

## Non-goals

- LIVE or any real-money change.
- AI in the decision path.
- Calendars.
- Commodity.
- Changing exits (except the optional DISC-C3 later).
- Pooling DISCOVERY evidence with STRICT evidence, or promoting from it.

## Context digest

- `STRICT` must behave exactly as before. Every change sits behind
  `entry_profile: DISCOVERY`.
- Protection-first legs, reservations, idempotency, recovery and the
  naked-short ban are unchanged.
- The 2026-09-25 evidence is the baseline:
  - 0 fills;
  - 30 `DEPTH_INSUFFICIENT` and 4 `SLIPPAGE_EXCEEDED`;
  - 24 `MIN_LOT_EXCEEDS_BUDGET` and 16 `EVENT_BLACKOUT`;
  - M4 `DATA_STALE` measured from the bar time;
  - M2 empty bind hidden as `INSTRUMENT_UNKNOWN`.

## Residual risks

- Touch fills overstate edge. The strict verdict on every order measures the
  gap.
- The 60-second poll can miss an intra-interval stop print (unchanged).
- The Fyers token must be refreshed daily before 09:00 IST.

## Unresolved decisions

None blocking. Per-mode entry caps (6/4/3/3) and max open positions
(2/2/4/4) are product defaults in `config/discovery.yaml`; the owner may
change them. Each change mints a new cohort.
