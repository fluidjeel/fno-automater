# Discovery Mode (PAPER only, temporary)

STATUS: ACTIVE from 2026-09-28 (owner decision 2026-09-26)
PROFILE: `DISCOVERY` (the current rule set is renamed `STRICT`)
ENVIRONMENT: PAPER only. Never selectable with `Environment.LIVE`.
BUILD STORIES: `docs/plans/DISCOVERY_MODE_STORIES.md`

## Why this exists

The strict rule set produced zero fills on 2026-09-25: 258 cycles, 34 paper
orders, 0 fills. Directional did not fire during a ~150-point NIFTY move
between 13:35 and 14:05 IST. Without trades we cannot learn:

- whether any mode has an edge;
- whether stops, trailing, targets and time exits work;
- which strict rules actually protect us.

Phase 1 goal: **find the edge.** Execution optimisation comes later.

**This is temporary.** The long-term target is the tighter `STRICT` rule set,
brought back one rule at a time as evidence arrives (see "Path back to strict
rules"). Every loosened rule keeps being evaluated on every decision, so we
always know which trades strict rules would have blocked.

## What does not change

These stay exactly as they are in `SAFETY_INVARIANTS.md`:

1. PAPER only. No real-order broker path is reachable.
2. Deterministic code makes every decision. AI stays proposal-only, with no
   LLM in the decision path.
3. Strategies emit `TradeIntent` only. Layer 2 stays the only authority.
4. No naked short options. Multi-leg structures submit protective legs first.
5. Every open position has deterministic protection: stop, target, trailing,
   time and expiry exits. Stops only tighten. A position keeps the exit policy
   frozen at entry.
6. Recovery and reconciliation run before entries after any restart.
   Idempotency keys, capital reservation before submit, and the
   unknown-outcome freeze all stay.
7. Money in `Decimal`, time zone-aware, config versioned.
8. Calendars stay `SUSPENDED` (loss bound across two expiries is unproven).

## Four independent books

Each mode is its own paper account:

- Each mode starts with **₹7,00,000**: M1, M2, M3 and M4 each get ₹7L.
- A mode's profit, loss, positions and open risk never affect another mode.
  There is no cross-mode borrowing, netting, overlap, conflict or
  duplicate blocking.
- **Daily compounding:** at session start, the mode equity is ₹7,00,000 plus
  that mode's realised net P&L after charges, up to the previous close. That
  figure stays fixed for the whole day. Profits grow the next day's budgets
  and losses shrink them.
- The existing `capital_share` split is not used in DISCOVERY.

## Priority order for every trade

1. **Setup.** Does the mode's logic see a trade?
2. **Strike selection.** Pick the best contract by the mode's selection rules.
3. **Fill.** Assume a normal market fill (below).
4. **Size.** Size last. Risk never changes the strike, expiry or structure
   that selection picked.

## Sizing (flexible)

The guide is a share of **that mode's** current equity:

| Mode | Per-trade guide | Open-risk cap (per mode) | New entries per day | Max open positions |
| --- | --- | --- | --- | --- |
| M1_CAS | 1% | 12% | 6 | 2 |
| M2_DIRECTIONAL | 2% | 12% | 4 | 2 |
| M3_TACTICAL_POSITIONAL | 2% | 12% | 3 | 4 |
| M4_STRATEGIC_POSITIONAL | 3% | 12% | 3 | 4 |

How lots are set:

- Lots equal the guide divided by the one-lot maximum loss, rounded down,
  with a **minimum of 1 lot**.
- If one lot already costs more than the guide, the system still takes 1 lot
  and tags it `ONE_LOT_OVER_GUIDE`.
- **Bug guard:** one trade may not risk more than 10% of mode equity (₹70,000
  at start). Anything larger is almost certainly a data or pricing bug. It is
  blocked with a reason.
- A new entry is blocked when that mode's open risk would pass 12% of its
  equity.
- **No daily loss stop** in DISCOVERY. The manual kill switch and
  `new_entries_enabled: false` still work. A Telegram alert (no block) fires
  when a mode's equity falls 20% below ₹7L.

The per-trade guide, open-risk cap, trade caps and bug guard are config
values, not code constants.

## Fills: assume a normal market

- A paper BUY fills at the displayed ask and a SELL at the displayed bid, plus
  charges.
- There is no depth requirement and no "must trade through" requirement.
- Every paper order also records what the strict fill model
  (`conservative-v1`) would have done: `FILLED`, `DEPTH_INSUFFICIENT` or
  `SLIPPAGE_EXCEEDED`. We learn the execution gap without blocking the trade.
- If there is no bid or ask at all, the order does not fill. That is missing
  data, not a strict rule.

## Hard rules vs soft rules

**Hard rules still block.** Each block is recorded with its reason:

- Environment is not PAPER.
- Naked short, undefined-loss structure, or wrong leg order.
- No usable price: bid, ask or LTP missing or zero, or the quote is older
  than 5 minutes (measured from the quote timestamp, not the 5-minute candle).
- Contract not in the instrument master, or lot size unknown.
- 0-DTE for a new entry in any mode. M2 also skips 1-DTE.
- Recovery or reconciliation incomplete, storage failure, or unknown order
  outcome.
- Outside the mode's entry window. M1 time exit stays at or before 15:25.
- Bug guard, per-mode open-risk cap, daily entry cap, max open positions.
- An identical position (same contracts, same side) is already open in the
  same mode.
- The family is `SUSPENDED` (calendars), or entries are disabled manually.

**Soft rules no longer block.** They are still evaluated and recorded as
`strict_would_block`:

- Event blackout and event caution.
- `MIN_LOT_EXCEEDS_BUDGET` under strict budgets.
- Paper fill depth, trade-through and slippage.
- Strict freshness rules beyond the hard 5-minute quote rule.
- Strict contract filters, which widen to a fallback band:
  - Delta: strict band preferred, then widen.
  - Open interest minimum.
  - Spread %.
  - DTE window.
  - Strikes scanned.
- Indicator warmup shortfalls. Use what is available and tag it.
- Router score thresholds and the 30-minute cooldown.
- Portfolio overlays:
  - Directional agreement, net delta and vega.
  - Tail budget.
  - Concentration and expiry-day notional.
  - Premium budget and strategy allocation fractions.
- Campaign drawdown freeze and account daily-loss cap.
- Cross-mode duplicate, overlap and conflict checks.
- G2 lifecycle-proof stance gates (straddle and strangle move to PAPER).
- M1 latency and "provider event only" rules. M1 may also fire from the
  60-second poll, tagged `M1_SOURCE=POLL`.

The strict preferred value is always tried first. The fallback is used only
when nothing passes strict. When it is used, the trade is tagged with the
exact rule and value that was relaxed.

## Record every decision and its reasoning

For every cycle, every mode and every family the system writes a durable
decision record, whether or not it trades. Each record holds:

- the decision: `TRADE`, `NO_TRADE` or `BLOCKED_HARD`;
- the reason codes plus one plain-English sentence explaining why;
- the market inputs used: spot, trend and direction, IV, event state and data
  ages;
- the candidate contracts considered, and why each lost or was rejected;
- sizing math: equity, guide, one-lot loss, lots and tags;
- the fill assumption and the strict fill verdict;
- `strict_would_block`: every soft rule that would have stopped this trade
  under `STRICT`;
- the exit plan frozen at entry (stop, target, trailing, time exit).

Exits get the same treatment: which rule fired, prices and P&L. An
end-of-day report summarises, per mode:

- trades taken and missed opportunities;
- P&L and equity;
- a count of each soft rule's "would have blocked" hits, with the P&L of those
  trades.

## Evidence labelling

- Experiment ids use the prefix `EXP-DISC`. DISCOVERY evidence is never pooled
  with `STRICT` evidence.
- DISCOVERY results cannot support any LIVE promotion. LIVE promotion
  requires a `STRICT` cohort under `PAPER_VALIDATION.md`.
- Any change to a DISCOVERY threshold starts a new cohort.

## Path back to strict rules

Review each mode after 30 closed trades or 4 weeks, whichever comes first. For
each soft rule, compare trades it would have blocked against trades it would
have allowed. A rule comes back (tightens) when the blocked group:

- has lower net expectancy; or
- has a worse drawdown.

It stays soft when blocking would have removed winners. Planned order:

1. Realistic fills (`conservative-v1`) become the default fill model.
2. Event blackout.
3. Contract filters (delta, OI, spread) narrow back toward strict values.
4. Sizing returns to strict per-trade budgets and fixed caps.
5. Daily loss stop and portfolio overlays return.

Each step is an owner-approved config change with a new cohort. DISCOVERY
ends when all soft rules are either restored or retired with evidence.
