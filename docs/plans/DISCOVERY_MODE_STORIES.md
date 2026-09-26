# Discovery Mode — Build Stories for Antigravity

Owner: product (this document). Builder: Antigravity.
Rulebook (canonical): `docs/context/DISCOVERY_MODE.md`. Read it first.
Repo: `/Users/apple/Documents/manasjit/fno-automated`. Oracle PAPER host runs
`fno-paper-session`.
Target: Sprint A live in PAPER before **Monday 2026-09-28 09:15 IST**.

---

## 0. Plain-language summary

On 2026-09-25 the paper system ran 258 cycles, sent 34 orders and got **zero
fills**. Directional stayed silent through a ~150-point NIFTY move. Everything
was blocked by strict rules meant for real money.

We are switching PAPER to a temporary **DISCOVERY** profile with these rules:

- It takes trades whenever a mode sees a setup.
- It picks the best strike first and sizes last.
- It fills at the displayed price.
- It records every decision and the reason behind it.
- It still honours hard safety: stops, exits, no naked shorts, PAPER only.

Each of the four modes gets its own independent ₹7L book that compounds daily.
Every loosened rule is still checked and logged as `strict_would_block`, so we
can bring the tight rules back one by one using evidence.

---

## 1. Rules for the builder (read before any story)

1. **Canonical order.** Follow `.cursor/rules/00-core.mdc`,
   `docs/context/SAFETY_INVARIANTS.md` (including the PAPER discovery
   exception) and `docs/context/DISCOVERY_MODE.md`. Never implement from
   `docs/research/`.
2. **The `STRICT` profile must behave exactly as today.** Every change is
   behind `entry_profile: DISCOVERY`. The existing tests stay green
   unmodified, except the ones a story explicitly names.
3. **No LIVE path.** `DISCOVERY` combined with `Environment.LIVE` must fail
   startup validation. No story may make a real broker adapter selectable.
4. **Config, not constants.** Every threshold named below lives in a new
   `config/discovery.yaml`, validated by a Pydantic model. Hard-coded values
   found during analysis must move to config:
   - M2 delta band `0.45–0.65` (`binders.py:~1354`);
   - `MAX_M4_OPEN_POSITIONS = 2` (`portfolio/economic_overlap.py:23`);
   - `MAX_SNAPSHOT_AGE_SECONDS` in strategies;
   - `_FOLLOWING_WEEK_STRIKES = 8` (`paper_session.py:~1471`);
   - `Decimal("700000")` in `paper_session.py:654–656` and `:1348`.
5. **Do not loosen:**
   - protection-first entry leg order;
   - liability-first exit (`paper_runner._liability_first`);
   - OMS stop-after-first-reject (`oms/engine.py:114–121`);
   - reservation before submit;
   - idempotency keys;
   - the unknown-outcome freeze;
   - recovery and reconciliation before entries;
   - the naked-short ban (`gateway._short_is_admissible`);
   - stop monotonicity;
   - frozen exit policy per position;
   - calendars `SUSPENDED`.
6. **Style.** Python 3.12, `mypy --strict`, `ruff` clean, `Decimal` money,
   injected clock, one-line docstrings, minimal patches.
7. **Story done means:**
   - the acceptance criteria are met;
   - new tests exist and fail before and pass after;
   - `uv run ruff check . && uv run mypy && uv run pytest` is green;
   - `docs/context/CURRENT_STATE.md` is updated in one or two lines;
   - the story row in `docs/plans/TASK_LEDGER.md` is set to `DONE`.
8. **Order.** Build stories in the order listed. Each story is one vertical
   slice. Do not start the next story until the current one is done.

---

## 2. Shared design (implemented across stories)

### 2.1 Profile switch

- `config/paper_session.yaml` gains `entry_profile: STRICT | DISCOVERY`
  (default `STRICT`) and `experiment_prefix: EXP-DISC` under DISCOVERY.
- `config/discovery.yaml` holds all DISCOVERY values. A sketch:

```yaml
schema_version: "1"
profile_version: "discovery-v1"
books:
  starting_equity_per_mode: "700000"     # each mode independent
  compounding: DAILY_REALIZED_NET        # equity fixed at session start
  drawdown_alert_fraction: "0.20"        # Telegram only, never blocks
modes:
  M1_CAS:                 {per_trade_guide: "0.01", open_risk_cap: "0.12", max_new_entries_per_day: 6, max_open_positions: 2}
  M2_DIRECTIONAL:         {per_trade_guide: "0.02", open_risk_cap: "0.12", max_new_entries_per_day: 4, max_open_positions: 2}
  M3_TACTICAL_POSITIONAL: {per_trade_guide: "0.02", open_risk_cap: "0.12", max_new_entries_per_day: 3, max_open_positions: 4}
  M4_STRATEGIC_POSITIONAL:{per_trade_guide: "0.03", open_risk_cap: "0.12", max_new_entries_per_day: 3, max_open_positions: 4}
bug_guard_trade_risk_fraction: "0.10"
hard_quote_max_age_ms: 300000
min_dte_new_entry: {default: 1, M2_DIRECTIONAL: 2}
fills: {model: "touch-v1", shadow_model: "conservative-v1"}
soft_reason_codes: [EVENT_BLACKOUT, MIN_LOT_EXCEEDS_BUDGET, DEPTH_INSUFFICIENT,
  SLIPPAGE_EXCEEDED, SPREAD_TOO_WIDE, SETUP_COOLDOWN, WARMUP_INCOMPLETE,
  RISK_LIMIT_DAILY_LOSS, RISK_LIMIT_STRATEGY, CONCENTRATION_LIMIT,
  CAMPAIGN_LOSS_LIMIT, EXACT_DUPLICATE_SUPPRESSED, ECONOMIC_OVERLAP_SUPPRESSED,
  OPPOSING_EXPOSURE_REJECTED, M4_POSITION_CAP_REACHED, DATA_DEGRADED]
selection:            # strict first, fallback only if nothing passes strict
  m2_delta:     {strict: ["0.45","0.65"], fallback: ["0.30","0.70"]}
  m1_delta:     {strict: ["0.20","0.40"], fallback: ["0.10","0.50"]}
  long_delta:   {strict: ["0.45","0.60"], fallback: ["0.30","0.70"]}
  short_delta:  {strict: ["0.20","0.35"], fallback: ["0.10","0.45"]}
  min_open_interest: {strict: 1000, fallback: 100}
  max_spread_fraction: {strict: "0.05", fallback: "0.10"}
  weekly_dte: {strict: [5, 12], fallback: [2, 14]}
  near_strikes_each_side: 5
  following_week_strikes_each_side: 10
direction:
  fallback_trend_threshold: "0.30"   # used when strict trend is not UP/DOWN
  fallback_min_bars: 14
```

### 2.2 Gate verdicts

- Introduce one small pure helper, for example
  `trading/risk/gate_profile.py::classify(reason, profile) -> HARD | SOFT`.
- Under `STRICT` everything is HARD, which is today's behaviour.
- Under `DISCOVERY` the codes listed in `soft_reason_codes` are SOFT. A SOFT
  verdict never rejects. It is appended to `strict_would_block` on the
  decision record.
- Hard codes in DISCOVERY are everything else, notably:
  - `PRICE_UNAVAILABLE`, `INSTRUMENT_UNKNOWN`, `INSTRUMENT_MASTER_ABSENT`;
  - `CONTRACT_EXPIRED`, `EXPIRY_0_1_DTE_EXCLUDED`;
  - `SYSTEM_NOT_READY`, `RECONCILIATION_UNRESOLVED`;
  - `KILL_SWITCH_ACTIVE`, `ENTRY_FROZEN`, `PROTECTION_DEGRADED`;
  - `RISK_LIMIT_TRADE` (naked short or bug guard);
  - `MODE_FAMILY_NOT_PERMITTED`, `CALENDAR_EXPERIMENTAL_OFF_STRICT_BOOK`;
  - `SNAPSHOT_MISMATCH`, `DECISION_EXPIRED`, `CAPITAL_UNAVAILABLE`;
  - `DATA_STALE` / `DATA_INVALID` from the hard quote-age rule.

### 2.3 Decision record

- Add a new durable event `DISCOVERY_DECISION`. It is either a new
  `TradingEventType` or a table. It is written for every
  (cycle, mode, family) evaluation and every exit.
- Fields:
  - `decision`: `TRADE | NO_TRADE | BLOCKED_HARD`;
  - `stage`: `DATA | DIRECTION | BIND | STRATEGY | ARBITER | RISK | FILL | EXIT`;
  - `reason_codes`, `reason_text` (one sentence, deterministic template);
  - `inputs`: spot, trend, fallback-trend flag, IV bucket, event state, data
    ages;
  - `candidates` (each with delta, OI, spread and rejection reason);
  - `sizing`: mode equity, guide, one-lot loss, lots, tags;
  - `fill`: assumed price, strict verdict;
  - `strict_would_block`, `experiment_id`, `profile_version`, `code_version`.
- `reason_text` is built from a fixed template. It never comes from an LLM.

---

## 3. Sprint A — must be live Monday 09:15 IST

### DISC-A0 — Profile switch and safety guard

Layer: runtime and config. Modes: all.

- **Why:** every later story hangs off one switch. The STRICT profile must
  keep working.
- **Change:**
  - Add `config/discovery.yaml` and its model under `trading/config/`.
  - Add `entry_profile` to the paper session config.
  - Extend `startup_validation.py`.
  - The startup banner prints `ENTRY PROFILE: DISCOVERY (temporary)`.
- **Acceptance:**
  - Given `entry_profile: DISCOVERY` and `Environment.PAPER`, when the
    session starts, then validation passes and the heartbeat JSON contains
    `entry_profile` and `profile_version`.
  - Given `DISCOVERY` with `Environment.LIVE` or a real broker adapter, then
    startup raises `StartupValidationError`.
  - Given `entry_profile` absent, then the behaviour equals today's; the
    full existing suite is green unchanged.
  - Given a malformed `discovery.yaml` (for example a fraction > 1 or a
    missing mode), then startup fails closed with a clear message.

### DISC-A1 — Four independent books with daily compounding

Layer: 2 (capital). Modes: M1–M4.

- **Why:** the owner wants each mode to stand alone with ₹7L and compound on
  its own results. Today `FourModeBook` splits one ₹7L by `capital_share`.
  Broker funds are hard-coded at 700k.
- **Change:**
  - `risk/mode_ledger.py`: under DISCOVERY, `allocated_capital` equals
    `starting_equity_per_mode + mode_realized_net_through_previous_close`.
    `reconstruct_from_store` already folds `prior_realized_net`; extend it
    rather than fork it.
  - `paper_session.py:654–656`: `BrokerFunds` equals the sum of the four
    mode equities.
  - `:1348`: M1 capital comes from the M1 book, not `share × 700000`.
  - `limits.build_sizing_limits` uses the mode's own equity.
- **Acceptance:**
  - Given a fresh store, then each mode shows ₹7,00,000 equity at session
    start.
  - Given M2 realised net +₹12,000 and M4 −₹5,000 yesterday, then today M2 is
    ₹7,12,000, M4 is ₹6,95,000, and M1/M3 are unchanged.
  - Intraday P&L does not change today's equity; it rolls in at the next
    session start.
  - Given an M4 loss, then M2's budget and open-risk room are unchanged (no
    cross-mode effect).
  - Restart mid-day reproduces the same equities, which are derived from the
    store, not memory.
  - Under STRICT, the existing 10/20/30/40 (28/32) behaviour is unchanged.

### DISC-A2 — Sizing: strike first, size last, 1-lot minimum

Layer: 2 (sizing, limits, gateway). Modes: all.

- **Why:** 24 `MIN_LOT_EXCEEDS_BUDGET` rejects on Friday. At strict budgets
  M4 has ₹2,240 per trade, which is less than almost any structure.
- **Change:**
  - In `risk/sizing/*` and `limits.py`, under DISCOVERY:
    - `lots = max(1, floor(guide × mode_equity / one_lot_max_loss))`;
    - when one lot is over the guide, tag `ONE_LOT_OVER_GUIDE` and record the
      strict `MIN_LOT_EXCEEDS_BUDGET` in `strict_would_block`.
  - Hard blocks:
    - bug guard: one-trade max loss > `bug_guard_trade_risk_fraction ×
      mode_equity` gives `RISK_LIMIT_TRADE`;
    - mode open risk after the trade > `open_risk_cap × mode_equity` gives
      `RISK_LIMIT_PORTFOLIO`.
  - These become soft under DISCOVERY:
    - the global ₹35k cap (G-27);
    - the premium budget (G-23);
    - account concurrent trades = 3 (G-24), replaced by the per-mode
      `max_open_positions`;
    - concentration (G-25);
    - exposure overlays (G-29);
    - the per-mode daily budget (G-21).
  - Sizing never alters strike, expiry or structure.
- **Acceptance:**
  - Given M4 mode equity ₹7L (guide 3% = ₹21,000) and an iron condor with
    one-lot max loss ₹6,000, then 3 lots are approved.
  - Given a long straddle with one-lot cost ₹24,000 (over the guide), then
    1 lot is approved, tagged `ONE_LOT_OVER_GUIDE`, and `strict_would_block`
    contains `MIN_LOT_EXCEEDS_BUDGET`.
  - Given a one-lot max loss of ₹75,000 at ₹7L equity, then it is rejected
    with `RISK_LIMIT_TRADE` and reason text naming the bug guard.
  - Given M2 open risk ₹80,000 and a new ₹10,000 trade (cap ₹84,000), then it
    is rejected with `RISK_LIMIT_PORTFOLIO`. M3 remains able to trade.
  - Reservation still happens before submit. Concurrent proposals cannot
    overspend the per-mode cap (reuse the `try_reserve` tests).

### DISC-A3 — Event blackout and daily-loss freezes become soft

Layer: 1 (P0), 2 (gateway, safety). Modes: all.

- **Why:** 16 `EVENT_BLACKOUT` rejects on Friday. The owner chose no daily
  stop for DISCOVERY.
- **Change:**
  - `gateway._constraint_reason` event branch (`gateway.py:753–770`) gives
    SOFT.
  - The paper P0 `EVENT_STATE` field (`safety/paper_data.py:271–287`) gives
    SOFT.
  - The account daily-loss kill switch and mode daily-loss freeze
    (`safety/controls.py:285–314`, `417–445`) do not trigger under DISCOVERY.
    They are still evaluated and logged as `strict_would_block`.
  - The M1 ledger daily-loss 15% check (`cas_event_path.py:595–621`) gives
    SOFT.
  - The campaign loss limit (G-28) gives SOFT.
  - The manual kill switch, `new_entries_enabled: false`, and protection or
    recovery freezes stay HARD.
  - Add a drawdown alert: when mode equity is below `(1 −
    drawdown_alert_fraction) × 700000`, send one Telegram per day. It never
    blocks.
- **Acceptance:**
  - Given event state `BLOCK_NEW_ENTRIES`, then under DISCOVERY the trade is
    approved and `strict_would_block` contains `EVENT_BLACKOUT`. Under STRICT
    it is rejected, as today.
  - Given a mode down 5% today, then entries continue and the record carries
    `RISK_LIMIT_DAILY_LOSS` as soft.
  - Given a manual kill switch, then no entry, in both profiles.
  - Given M3 equity ₹5,55,000, then exactly one drawdown Telegram that day.

### DISC-A4 — Paper fills at the displayed price, with the strict verdict recorded

Layer: 2 (paper broker) and 4 (fill model). Modes: all.

- **Why:** all 34 approved orders were rejected by `conservative-v1`:
  - 30 `DEPTH_INSUFFICIENT`;
  - 4 `SLIPPAGE_EXCEEDED` ("market did not trade through the limit").
- **Change:**
  - Add a `touch-v1` fill model: BUY at the displayed ask, SELL at the
    displayed bid, plus `charges_per_lot`. No depth or trade-through
    requirement.
  - Missing bid or ask still gives `PRICE_UNAVAILABLE`. That is HARD.
  - `broker/paper/adapter.py`: under DISCOVERY, fill with `touch-v1` and also
    run `analytics/fills.simulate_fill` with `conservative-v1`. Store its
    outcome and reason as `strict_fill_verdict` on the order and fill events.
  - `paper_runner.py:343–347` model check: accept the touch plus shadow pair.
  - Multi-leg: legs still submit protection-first and the OMS still stops on
    the first reject.
  - The cohort `fill_model_version` is `touch-v1`, so the scorecard refuses
    to pool it with `conservative-v1`.
- **Acceptance:**
  - Given ask 120.50, bid 119.50, ask size 0 and last 118, then a BUY limit
    121 fills at 120.50 plus charges. `strict_fill_verdict` is
    `DEPTH_INSUFFICIENT`.
  - Given bid missing, then a SELL is rejected with `PRICE_UNAVAILABLE`.
  - Given an iron condor, then the fill order is long wings before shorts.
    Exits buy back the shorts first.
  - STRICT still uses `conservative-v1` alone.
  - `trading evaluate scorecard` on a DISCOVERY cohort reports fill model
    `touch-v1` and a count of strict verdicts.

### DISC-A5 — Staleness measured from the quote, not the 5-minute candle

Layer: 1 and 3. Modes: M2, M3, M4 (and every strategy with an age check).

- **Why:**
  - M4 was `DATA_STALE` in 176 cycles. `iron_condor.py:106–112` and
    `m4_broad_basket.py:93–99` compare now against `underlying.times.event_time`,
    which is often the last 5-minute bar, against a 120 s limit.
  - Quotes were fresh.
- **Change:**
  - All strategies measure age from the freshest quote or chain time
    (`calculation_time` or an explicit `quote_time`). They do not use bar
    `event_time`.
  - Move `MAX_SNAPSHOT_AGE_SECONDS` constants to config. Under STRICT they
    keep today's values: 120 s, and 30 s for CAS.
  - Under DISCOVERY the hard rule is `hard_quote_max_age_ms` (5 minutes).
    Anything between the strict limit and 5 minutes is SOFT `DATA_STALE`,
    tagged.
  - Fix for both profiles: using bar `event_time` for quote freshness is a
    measurement bug.
- **Acceptance:**
  - Given a quote 20 s old and the last bar 4 minutes old, then an M4
    strategy does not reject `DATA_STALE` under either profile.
  - Given a quote 3 minutes old, then DISCOVERY trades with soft `DATA_STALE`
    and STRICT rejects.
  - Given a quote 6 minutes old, then both profiles reject HARD.

### DISC-A6 — Directional (M2) fires on real moves

Layer: 1 (market state), identification (binder), 3 (`long_option`). Mode: M2.

- **Why:** on Friday 13:35–14:05 IST, NIFTY moved ~150 points with
  `positional_long_option` giving 0 intents and `INSTRUMENT_UNKNOWN`. The
  binder returned zero contracts. Its real reason was hidden. Possible
  causes, per `binders.py:1264–1372` and `market_state.py:229–246`:
  - trend not UP/DOWN: needs warmup of 50 bars and 20 sessions plus VIX, a
    score ≥ 0.60, and 15 m and 60 m returns with the same sign;
  - the delta band `0.45–0.65` is hard-coded;
  - OI, spread, top-of-book or greeks filters.
- **Change:**
  - **Direction fallback (DISCOVERY):** if strict trend is not UP/DOWN, use
    the 15 m and 60 m returns. When both have the same sign and |score| ≥
    `direction.fallback_trend_threshold` (0.30), with at least
    `fallback_min_bars` bars, the direction is UP or DOWN. Tag
    `DIRECTION_FALLBACK`. If warmup is incomplete, use available bars and
    record soft `WARMUP_INCOMPLETE`.
  - **Two-pass contract pick:** pass 1 uses the strict values; pass 2 uses
    `selection.*` fallback values. Delta moves to config. Pick the contract
    whose |delta| is closest to 0.55 among the survivors of the first pass
    that has any.
    - Tag each relaxed field, for example `M2_DELTA_FALLBACK=0.38`.
  - **Strike feed:** `option_strikes_each_side` goes to 5 near under
    DISCOVERY, and following-week strikes to 10 (from config).
  - **Expiry:** following week preferred, fallback allowed
    (`allow_fallback_expiry=True`). The hard minimum is 2 DTE.
  - **Wiring:** `four_mode_producers` passes `bound.binding.reason_codes`
    through to the request and outcome, so an empty bind records the true
    binder reason, not `INSTRUMENT_UNKNOWN`. Also fix the misnamed
    `PRICE_UNAVAILABLE` when direction is None: emit a
    `DIRECTION_UNRESOLVED` reason code.
  - `long_option.py` silent abstains (lines 118–119 and 123–124) must emit
    reason codes: `DIRECTION_NEUTRAL` and `OPTION_TYPE_MISMATCH`.
- **Acceptance:**
  - Given a fixture replaying a +150-point rise over 30 minutes of 5 m bars,
    with strict trend `MIXED`, then under DISCOVERY M2 emits one `long_call`
    intent tagged `DIRECTION_FALLBACK`. Under STRICT it abstains with
    `DIRECTION_UNRESOLVED` (not `INSTRUMENT_UNKNOWN`).
  - Given only strikes with |delta| 0.38 and 0.72, then DISCOVERY binds 0.38
    tagged `M2_DELTA_FALLBACK`. STRICT returns empty with the binder reason
    persisted.
  - Given the following week is missing and the next listed expiry is 3 DTE,
    then DISCOVERY binds it, tagged. Given 1 DTE, then HARD reject.
  - Given zero candidates for any reason, then the cycle record contains the
    binder's reason code and one-line text.
  - If Oracle has the stored 2026-09-25 bars, the builder runs a replay of
    13:30–14:10 and attaches the decision records to the story PR. Either a
    trade fires or a named hard reason is shown.

### DISC-A7 — Straddles and strangles to PAPER; M4 capacity from config

Layer: runtime (routing), 2 (arbiter). Mode: M4.

- **Why:**
  - `family_gates.py:13–18` hard-blocks `long_straddle` and `long_strangle`
    (G1), and startup refuses PAPER for them.
  - `MAX_M4_OPEN_POSITIONS = 2` is a code constant.
  - Also found: the `G2_UNPROVEN_FAMILIES` names (`iron_condor`,
    `credit_spread`, `defined_risk_multileg`) do not match the live
    `FamilyId` values, so that gate silently never fires.
- **Change:**
  - Under DISCOVERY the G1 and G2 family sets are SOFT (recorded, not
    blocking) in both `startup_validation.py` and
    `session_routing.family_executable`.
  - Set `family_stances` for straddle and strangle to `PAPER` in
    `config/paper_session.yaml`.
  - Calendars stay blocked in both profiles.
  - Move the M4 cap to config; the DISCOVERY value comes from `max_open_positions`.
  - Fix the G2 name mismatch in STRICT: map to real `FamilyId`s. Add a test
    so the set cannot drift again.
- **Acceptance:**
  - Given DISCOVERY and a straddle stance of PAPER, then startup passes and a
    bound straddle produces an executable request.
  - Given STRICT, then startup still refuses PAPER straddles.
  - Given calendars set to PAPER in any profile, then startup fails.
  - Given DISCOVERY with 3 M4 positions open and `max_open_positions: 4`,
    then a 4th is allowed and a 5th is rejected.
  - A test asserts every name in the G1 and G2 sets is a valid `FamilyId`.

### DISC-A8 — Duplicates and overlaps only within a mode; daily entry caps

Layer: 2 (arbiter). Modes: all.

- **Why:** with independent books, cross-mode overlap must not block.
  Without cooldowns, the system needs a simple anti-spam rule.
- **Change:**
  - In `portfolio/arbitration.py` under DISCOVERY:
    - Exact duplicate is compared only against open or pending positions of
      the same mode. It stays HARD within the mode: the same contracts and
      side cannot be stacked every 60 s.
    - Economic overlap and opposing exposure (A-03/A-04) become SOFT and are
      recorded.
  - Router and winner cooldown (`cooldown_minutes`, `SETUP_COOLDOWN`) become
    SOFT.
  - New HARD caps per mode: `max_new_entries_per_day` and
    `max_open_positions`.
  - Arbiter suppressions are persisted with their real reason code.
    `paper_runner.py:2498–2508` currently collapses them to
    `EXACT_DUPLICATE_SUPPRESSED`.
- **Acceptance:**
  - Given M2 long 25000 CE open, then M3 may open a bull call spread with the
    25000 CE long leg, and an M2 re-entry of the same CE is rejected.
  - Given M2 has made 4 entries today, then the 5th is rejected with reason
    `DAILY_ENTRY_CAP`, and M3 is unaffected.
  - Given an overlap suppression, then the stored reason is
    `ECONOMIC_OVERLAP_SUPPRESSED`, not the duplicate code.

### DISC-A9 — Record every decision and its reasoning

Layer: 3, runtime, 4. Modes: all.

- **Why:** today three things are lost:
  - `Rejection.detail` is dropped (`paper_runner.py:2431`);
  - binder reasons are dropped (`four_mode_producers.py:168–171`);
  - silent `return decision` paths write nothing. These are in
    `long_option.py`, `cas_microstructure.py`, `debit_spread.py`,
    `multileg_options.py` and `directional_conviction.py`.

  Declined signals with no code never reach the cohort (`cohort.py:120`). We
  cannot learn from what we cannot see.
- **Change:**
  - Implement the §2.3 `DISCOVERY_DECISION` record. Write one record per
    (cycle, mode, family) and per exit, in both profiles; it is cheap and
    STRICT benefits too.
  - Every silent abstain gets a reason code and detail. New codes as needed:
    `DIRECTION_NEUTRAL`, `OPTION_TYPE_MISMATCH`, `MICROSTRUCTURE_UNCONFIRMED`,
    `CONVICTION_BELOW_THRESHOLD`, `REGIME_NOT_RANGE`, `VOL_COMPRESSED`.
  - Persist `Rejection.detail` into `StrategyCycleSummary`, as an optional
    field for schema compatibility.
  - Funnel drops use the real detail, not the fixed string at
    `activity_funnel.py:63`.
  - The cohort writes declined rows for zero-intent evaluations too.
  - `reason_text` templates live in one module (for example
    `runtime/decision_text.py`) with one template per code.
  - Add a CLI: `trading evaluate decisions --date YYYY-MM-DD [--mode M2]`
    prints a readable table.
- **Acceptance:**
  - For one poll cycle with 4 modes and N families, the number of records
    equals the number of evaluated (mode, family) pairs. None has an empty
    `reason_codes` unless `decision=TRADE`.
  - Given M2 with NEUTRAL direction, then the record says, for example,
    "No trade: direction neutral (15m +0.02%, 60m −0.05%, score 0.11 below
    0.30)."
  - Given a trade, then the record lists every candidate with delta, OI and
    spread, and the winner, sizing math, fill and strict verdict, and
    `strict_would_block`.
  - Given an exit, then the record names the rule that fired (stop, target,
    trail, time, review or carry), with prices and P&L.
  - Records survive restart and are queryable by date, mode and
    `experiment_id`.

### DISC-A10 — The session never crashes on a data error

Layer: runtime and 1. Modes: all.

- **Why:** a Fyers 401 raised out of the builder inside `tick()` and crashed
  the session at 09:16. The following-week fetch also hit a 401 later in the
  day.
- **Change:**
  - `paper_session.tick()` wraps `self._builder(now)`. On `FyersApiError`,
    `OSError`, a timeout or a `ValueError` from payload parsing:
    - log the error and write a `DISCOVERY_DECISION` with `stage=DATA`,
      `BLOCKED_HARD`, reason `DATA_FEED_ERROR`;
    - skip new entries for that cycle;
    - still run `manage_exits` using the protection coordinator's latest
      quotes;
    - continue the loop.
  - After 3 consecutive failures, send one Telegram. On recovery, send one
    Telegram.
  - `401` specifically: the Telegram says "Fyers token expired — refresh
    token".
  - The following-week fetch failure keeps the near chain and records the
    reason. This is already partly done in `_merge_following_week_chain`;
    make it explicit in the record.
- **Acceptance:**
  - Given the builder raises a 401 on cycles 1–2 and succeeds on cycle 3,
    then the process keeps running, 2 `DATA_FEED_ERROR` records exist, and
    cycle 3 evaluates normally.
  - Given an open position and a failing builder, then exits are still
    evaluated from protection quotes.
  - Given 3 failures, then exactly one alert is sent; after recovery, one
    "recovered" alert.

### DISC-A11 — Discovery cohort identity

Layer: 4 and runtime. Modes: all.

- **Why:** `experiment_id_for` uses prefix plus strategy plus ISO week. A
  mid-week rule change does not mint a new cohort.
- **Change:**
  - Under DISCOVERY the prefix is `EXP-DISC`. The experiment id also
    includes a short hash of `discovery.yaml` `profile_version` plus
    checksum.
  - The scorecard refuses to mix DISCOVERY and STRICT cohorts.
  - Eligibility on a DISCOVERY cohort always returns `INELIGIBLE` with the
    reason "discovery cohorts are not promotion evidence".
- **Acceptance:**
  - Changing any value in `discovery.yaml` produces a different
    `experiment_id` on the next cycle.
  - `trading evaluate eligibility <EXP-DISC-…>` returns `INELIGIBLE` with
    that reason.

### DISC-A12 — Deploy and Monday readiness

Layer: operations.

- **Change:**
  - Commit the tree, rsync to Oracle (owner approves the deploy), and run the
    bootstrap (ruff, mypy, pytest).
  - Refresh the Fyers token before 09:00 IST.
  - Restart `fno-paper-session`.
- **Acceptance:**
  - The startup log prints `ENTRY PROFILE: DISCOVERY (temporary)`, all four
    modes `PAPER`, straddle/strangle `PAPER`, and calendars `SUSPENDED`.
  - The heartbeat shows `entry_profile: DISCOVERY`.
  - By 09:30 IST, decision records exist for every mode each cycle.
  - The Oracle tree hash matches the commit.
  - Rollback is documented in `OPERATIONS_RUNBOOK.md`: set `entry_profile:
    STRICT` and restart. Positions opened under DISCOVERY keep their frozen
    exit policy.

---

## 4. Sprint B — first week of discovery

### DISC-B1 — M1 fires from the 60-second poll

Layer: runtime, identification, 3. Mode: M1.

- **Why:** M1 only runs on a provider event (`submit_m1_event`), and the poll
  clears pending events (`paper_session.py:403–405`). No in-market M1 trace
  exists yet.
- **Change:**
  - Under DISCOVERY, each poll inside the M1 window (09:20–15:25) builds an
    M1 evaluation from the REST quote (`quote_only` profile). It is tagged
    `M1_SOURCE=POLL`.
  - Provider events still run as today, tagged `M1_SOURCE=EVENT`.
  - Missing depth-based CAS features become SOFT `DATA_GAP` when the
    quote-only microprice signal is present. The quote-instability check
    remains.
  - Keep M1 time exit ≤ 15:25. The strict delta band is preferred, with
    `m1_delta` as fallback.
  - The latency report stays informational only.
- **Acceptance:**
  - Given a poll at 10:05 with a quote-only microprice edge ≥ 2 ticks, then
    an M1 intent is created, tagged `POLL`, and fills under `touch-v1`.
  - Given a poll at 15:26, then no M1 entry, HARD `OUTSIDE_SESSION`.
  - Given an M1 fill, then `time_exit` ≤ 15:25 IST.
  - M1 daily entry cap from config (6) is enforced.

### DISC-B2 — M3 and M4 contract picking with fallback

Layer: identification. Modes: M3, M4.

- **Change:**
  - Apply the two-pass strict-then-fallback selection (§2.1 `selection`) in:
    - `bind_debit_spread`;
    - `bind_credit_spread`;
    - `bind_iron_condor`;
    - butterflies;
    - straddle and strangle.
  - `_dte_ok` uses `weekly_dte.fallback` in pass 2.
  - The `_dominant_reason` fallthrough to `INSTRUMENT_UNKNOWN` must report
    the true failing filter, for example `DTE_OUT_OF_WINDOW`.
  - Setup logic is **not** loosened. Condors and butterflies still require
    RANGE and straddles still skip COMPRESSED. These are edge hypotheses; the
    record states them via `REGIME_NOT_RANGE` and `VOL_COMPRESSED`.
- **Acceptance:**
  - Given a bull call spread with the only long leg at |delta| 0.35 and 9
    DTE, then DISCOVERY binds it with tags. STRICT rejects with the true
    reason.
  - Given a condor on a trending day, then no trade, reason
    `REGIME_NOT_RANGE`, recorded.

### DISC-B3 — End-of-day discovery report and alerts

Layer: 4 and runtime (`notify.py`).

- **Change:**
  - An EOD Telegram and a markdown file under `data/paper/discovery/`. Per
    mode, it shows:
    - equity (start, end, tomorrow);
    - trades, wins, losses, net P&L;
    - open positions;
    - top no-trade reasons;
    - each soft rule's `strict_would_block` hit count with the P&L of those
      trades;
    - the strict fill verdict mix.
  - A "no trade by 11:00 IST" Telegram per mode, once per day, including the
    top 3 reasons from the records.
- **Acceptance:**
  - Given a session with 5 trades, then the report totals match the store.
    The soft-rule table sums to the trades that carry each tag.
  - Given M2 with no trade by 11:00, then one alert with the top reasons.

### DISC-B4 — Dashboard shows the discovery view

Layer: dashboard (read-only).

- **Acceptance:** the dashboard shows:
  - a DISCOVERY banner;
  - four book equities;
  - today's decisions per mode (TRADE/NO_TRADE with reason text);
  - open positions with their exit plan.

  It is read-only, with no live levers.

---

## 5. Sprint C — path back to strict rules

### DISC-C1 — Soft-rule evidence report

Layer: 4.

- **Change:**
  - Add a CLI: `trading evaluate soft-rules --cohort EXP-DISC-… [--mode]`.
  - For each soft rule it shows:
    - trades it would have blocked vs allowed;
    - count, net expectancy and max drawdown for each group;
    - a verdict: `TIGHTEN_CANDIDATE`, `KEEP_SOFT` or `INSUFFICIENT_SAMPLE`
      (fewer than 30 closed trades in the mode).
  - It is read-only and never writes config.
- **Acceptance:**
  - On a fixture cohort where event-blackout trades lose on average and
    others win, then the verdict is `TIGHTEN_CANDIDATE` for
    `EVENT_BLACKOUT`.
  - With fewer than 30 closed trades, then `INSUFFICIENT_SAMPLE`.

### DISC-C2 — Tightening is a config step with a new cohort

- **Acceptance:**
  - Moving a code out of `soft_reason_codes`, or narrowing a fallback, is a
    single config edit.
  - It produces a new `experiment_id` (via DISC-A11).
  - It is recorded in `CURRENT_STATE.md` with the evidence report that
    justified it.
  - Planned order: fills, then event blackout, then contract filters, then
    sizing, then daily stop and overlays.

### DISC-C3 (later, optional) — Trailing for M1 and spreads

Today only M2 trails, at 40/20 ticks. M1, M3 and M4 have stop, target and
review but no trail. Extending trailing tightens protection and is
compatible with the rulebook. It is scheduled after a week of exit evidence,
not before.

---

## 6. Coverage matrix (layers × modes)

| Area | M1 CAS | M2 Directional | M3 Verticals | M4 Basket |
| --- | --- | --- | --- | --- |
| L1 data: staleness, feed errors | A5, A10 | A5, A10 | A5, A10 | A5, A10 |
| L1 direction / regime | B1 | **A6** | B2 | B2 (setup unchanged) |
| Identification: strikes, fallback | B1 | **A6** | B2 | B2, A7 |
| L3 strategy: silent abstains | A9 | A6, A9 | A9 | A9 |
| Runtime: stance, routing, M1 poll | **B1** | — | — | **A7** |
| L2 books, compounding | A1 | A1 | A1 | A1 |
| L2 sizing, caps | A2, A8 | A2, A8 | A2, A8 | A2, A7, A8 |
| L2 event, daily loss | A3 | A3 | A3 | A3 |
| L2 paper fills | A4 | A4 | A4 | A4 |
| Exits: unchanged, recorded | A9 | A9 (trail, 15:20, carry) | A9 (reviews) | A9 (reviews) |
| L4 records, cohort, reports | A9, A11, B3, C1 | same | same | same |

## 7. Not in scope

- Any LIVE or real-money change.
- AI in the decision path.
- Calendars.
- Changing exit rules other than DISC-C3.
- Commodity.
- Rewriting the STRICT profile.
