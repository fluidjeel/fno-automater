# Four-mode layer and component change list

**Written:** 24 September 2026  
**Code status:** no runtime change in this file. The live paper path is still the one-winner NIFTY router plus a CAS side path.  
**Target:** [NIFTY_FOUR_MODE_CURSOR_REDESIGN.md](NIFTY_FOUR_MODE_CURSOR_REDESIGN.md) v1.1  
**Related:** [FOUR_MODE_COMPONENT_CHANGES.md](FOUR_MODE_COMPONENT_CHANGES.md), [FOUR_MODE_REDESIGN_PLAN.md](FOUR_MODE_REDESIGN_PLAN.md)

A component is done only when every success criterion under it is true in code and in a test or a labelled report. A class, a `ModeId` enum, or an affordability row is not enough.

Promotion ladder used below:

| Status | Means |
| --- | --- |
| `IMPLEMENTED_UNIT` | Structure and unit tests exist |
| `LIFECYCLE_PROVEN` | Entry, partial-fill recovery, monitoring, exit, and restart pass for that family |
| `PAPER_STANCE_ENABLED` | Config stance is PAPER only after `LIFECYCLE_PROVEN` |
| `one_lot_fits` | G1 says one complete structure fits that mode’s cap. It is not a PAPER grant |

---

## Layer 1 — data and calculations

Layer 1 stays free of trade selection, sizing, and orders. Required work is capability labels, calendar facts, and snapshots the mode engines can consume.

### 1.1 Fyers quotes, bars, chain, websocket

**Path:** `src/trading/data/fyers/`  
**Required change:** Keep the adapter. Tag each capture with provider time, receive time, and whether it is a live probe. Do not invent depth or trade flow.

**Success criteria:**

- A missing bid or ask is `MISSING`, never zero.
- A stored chain used while the market is closed is labelled `is_live_probe=false` with both timestamps.
- NIFTY index, NIFTY futures, and NIFTY option quotes stay distinct symbols.

### 1.2 Depth and CAS feature pipeline

**Path:** `src/trading/data/cas_depth/`, `src/trading/data/cas_features.py`, `src/trading/data/fyers/capability_probe.py`  
**Required change:** Inventory the fields the paper entry path actually consumes. Separate Mode 1 microstructure from NSE’s cash Closing Auction Session. Publish quote-only and depth-only profiles. Leave aggressor/trade-flow absent until a probe shows the field.

**Success criteria:**

- A written field table marks each input observed, inferred, or unavailable.
- `aggressor_side` stays unavailable while the capability map says false.
- Quote-only and depth-only runs produce different feature versions and different cohort labels.
- A 60-second poll is recorded as the entry-loop age. The 2-second protection poll is recorded as an open-position path only.
- Gate G3 ends as either a bounded event-driven M1 entry path or `BLOCKED` / quote-only. It does not enable M1 PAPER by itself.

### 1.3 Instrument master

**Path:** `src/trading/data/storage/instrument_store.py`, `data/reference/instruments/NSE_FO.jsonl`  
**Required change:** Lot, tick, expiry, and freeze quantity come from the current master for the specific contract, not from the first NIFTY option row and not from a remembered constant.

**Success criteria:**

- Two NIFTY option expiries can carry different lot sizes without the loader collapsing them.
- A missing spec fails closed.
- G1 and the sizers read the same spec for the contract they price.

### 1.4 Pipeline, cycle, and snapshot builder

**Path:** `src/trading/data/pipeline.py`, `cycle.py`, `snapshot_builder.py`, `src/trading/runtime/candidates.py`  
**Required change:** Build a coherent decision bundle: underlying, each option leg, feature version, calendar version, and capability profile. Widen the candidate chain beyond ATM ± 2 when a mode’s strike band needs it. Keep raw leg snapshot ids.

**Success criteria:**

- Two legs in one decision may have different snapshot ids and still pass coherence if skew and identity checks pass.
- Copying one parent id onto every leg fails a regression test.
- The bundle records max cross-instrument age.
- Candidate strike count is a mode input, not a single session constant of 2 for every family.

### 1.5 Data quality

**Path:** `src/trading/data/quality.py`, `DataQuality` in `src/trading/domain/enums.py`  
**Required change:** Add `MISSING` and `INCONSISTENT` when a producer can emit them. Mandatory invalid inputs block new exposure. Open positions go to degraded protection instead of a fake close.

**Success criteria:**

- A test shows stale or missing mandatory quotes block a new intent.
- The same quotes do not mark an open position closed.
- `DEGRADED` is explicit. It is not stored as `VALID`.

### 1.6 IV, Greeks, realized vol, VIX

**Path:** feature calculators used by `identification/` and `data/vix_backfill.py`  
**Required change:** Keep deterministic calculators. Publish units (delta sign, theta per day, vega per vol point). A missing delta stays missing.

**Success criteria:**

- A delta gate rejects a snapshot whose delta is absent.
- IV and realized-vol comparisons name their windows.
- No mode selector treats IV percentile as a win probability.

### 1.7 Replay and storage

**Path:** `src/trading/data/replay.py`, `data/storage/`  
**Required change:** Replay the same bundle, config, and code version to the same decision. DuckDB/Parquet stay analytics-side. The order/position store stays transactional.

**Success criteria:**

- Two replays of one fixture produce byte-identical risk decisions.
- Analytics jobs are not a second writer of the order database.

### 1.8 Macro and news ingestion

**Path:** `src/trading/data/macro_news.py`, `src/trading/news/`  
**Required change:** Keep collectors deterministic and separate from LLM interpretation. Event calendar blackouts stay hard. Sentiment cannot clear them.

**Success criteria:**

- A news payload containing instructions does not change tool permissions or risk limits.
- A missing optional macro feed is a different reason from a missing mandatory event calendar.
- No LLM call sits on the tick-to-order path.

### 1.9 Universe scanner

**Path:** `src/trading/universe/`  
**Required change:** Remove it from the NIFTY execution path. Keep it only if a future non-NIFTY research job still needs it, labelled out of this deployment.

**Success criteria:**

- The paper session does not call the scanner to choose a NIFTY option.
- `directional_conviction` is not registered as a second live strategy.

### 1.10 Calendar facts

**Path:** session windows in `config/identification.yaml`, `config/paper_session.yaml`, daemon close in `src/trading/ops/daemon.py`  
**Required change:** One calendar port: session date, phase, listed expiries, holidays, entry cutoff, flatten cutoff, source, effective date. Do not change 15:30 / 15:40 until an effective circular and a broker check are recorded.

**Success criteria:**

- M2 expiry selection uses a listed contract from the master, not a guessed weekday symbol.
- A special session outside 10:30 or 14:30 follows a written skip rule.
- Clock values in config match the verification note, or the note says they are still unverified and unchanged.

---

## Layer 2 — risk, capital, OMS, positions

Layer 2 remains the only authority that sizes, reserves, and submits.

### 2.1 Risk gateway

**Path:** `src/trading/risk/gateway.py`  
**Required change:** Accept a mode id and family id. Reject unknown mode/family pairs, non-NIFTY execution underlyings, and M3 single-leg entries. Classify actions by resulting exposure so a risk-reducing close is not blocked by entry filters. Credit and condor approval must place protection before short exposure.

**Success criteria:**

- A futures or non-NIFTY option intent is rejected with a stable reason code.
- An M3 long-call intent is rejected.
- An entry freeze still allows a valid exit.
- Debit, credit, and condor approved leg order is long protection before short.
- Every rejection code used here has a test.

### 2.2 Limits and capital accounts

**Path:** `src/trading/risk/limits.py`, `config/risk.yaml`, `config/paper.yaml`, `src/trading/runtime/paper_session.py` equity  
**Required change:** Four ledgers on the existing ₹7,00,000 book, shares 10/20/30/40. Reference capital is min(start-of-session allocation, conservative equity). No cross-mode borrow. Per-trade fractions 5% / 3% / 2% / 1% of that mode’s reference capital. G1 failures cannot reserve.

**Success criteria:**

- Restart restores each mode’s equity, reservations, and day loss.
- Mode A cannot spend Mode B’s cash.
- A one-lot cost above the cap returns `MIN_LOT_EXCEEDS_BUDGET` and zero lots.
- The test does not pass by raising the cap or using a fractional lot.
- Legacy open positions keep their stored owner and are not rewritten onto a new policy.

### 2.3 Reservations

**Path:** `src/trading/risk/reservation.py`  
**Required change:** Reservation rows carry mode id. Release only from confirmed events. Counterfactual evaluation must not call this module.

**Success criteria:**

- Duplicate reserve of the same idempotency key does not double-spend.
- A counterfactual test asserts the reservation store is unchanged.
- Crash after reserve and before submit restores the reservation or releases it through recovery, never both.

### 2.4 Sizers

**Path:** `src/trading/risk/sizing/`  
**Required change:** Keep the minimum-of-bounds method. Add sizers only for families that have a payoff check. Read charges from the same schedule as the fill model. Ignore strategy-requested ₹10,000 as authority.

**Success criteria:**

- Approved lots equal the minimum of risk, cash, margin, portfolio, liquidity, and structure-ratio bounds.
- Zero lots name the binding constraint.
- Credit max loss is (width − credit) × lot × structure lots plus charges.
- Long option max loss is premium plus charges.
- No sizer exists for a family that is PAPER.

### 2.5 G1 affordability

**Path:** `src/trading/risk/affordability.py`, `docs/reports/G1_ONE_LOT_AFFORDABILITY.md`  
**Required change:** Price the cheapest complete structure inside each family’s allowed strike and expiry band, from the contract’s own lot and a timestamped chain. Do not hardcode strikes. `one_lot_fits` must not be treated as PAPER. M2 must use the following-week band (about 0.45–0.65 absolute delta), not a cheaper 0.41 delta.

**Success criteria:**

- A chain without the previously hardcoded strikes still returns a status or an abstention, not `KeyError`.
- M2 reports `MIN_LOT_EXCEEDS_BUDGET` when every in-band strike exceeds the cap.
- The narrative is generated from the evaluation rows, not from copied rupee sentences.
- Event time comes from the capture, not a literal in source.
- The gateway does not read `executable=True` as permission to submit.

### 2.6 Snapshot bundle

**Path:** `src/trading/risk/snapshot_bundle.py`  
**Required change:** Keep distinct leg snapshot ids. Attach the bundle to the risk decision.

**Success criteria:**

- T08: distinct coherent leg ids pass.
- T09: a stale or skewed leg rejects and the original ids remain in the audit record.

### 2.7 Exposure and arbitration

**Path:** `src/trading/portfolio/exposure.py`, `src/trading/identification/router.py` (winner logic moves out), session correlation check in `paper_session.py`  
**Required change:** Replace the one-winner router and the “any open NIFTY position blocks” rule with: exact duplicate suppression, then economic overlap, conflict, and an M4 cap of two positions. One owner per position.

**Success criteria:**

- Two identical leg sets produce one executable order and a suppression row that names the incumbent.
- A bull call and a bull put with the same thesis are treated as overlap, not as two families that both trade.
- M1 and M2 may both be long only when incremental portfolio risk fits.
- Opposing theses without an explicit hedge are rejected.
- Gross tail risk can reject a book whose net delta is small.
- Combined stress for two M4 positions is checked before the second fill.

### 2.8 Portfolio view, reconciliation, risk journal

**Path:** `src/trading/portfolio/view.py`, `reconciliation.py`, `risk_journal.py`  
**Required change:** View and journal break P&L, margin, and risk by mode and in total. Internal fills allocate contracts to modes. Aggregate must match the paper broker.

**Success criteria:**

- A report of mode P&L plus unallocated residual equals broker P&L.
- Restart does not drop an open losing position or reset the day’s loss.
- The journal does not mutate reservations.

### 2.9 OMS, planner, rate limit

**Path:** `src/trading/oms/`  
**Required change:** Keep one idempotency key and UNKNOWN freeze. Submit legs in the gateway’s safe order. On a partial fill, cancel the rest, reconcile, then repair or flatten. Never treat a timeout as “no fill.”

**Success criteria:**

- UNKNOWN submit, process restart, and a second submit attempt produce one broker order.
- For every short-containing family, no tested prefix leaves a naked short.
- A limit order does not fill through its limit.
- Rate-limit deferral does not drop the idempotency key.

### 2.10 Paper broker and fill model

**Path:** `src/trading/broker/paper/adapter.py`, `src/trading/analytics/fills.py`, `config/evaluation.yaml`  
**Required change:** Keep buys at ask plus impact and sells at bid minus impact. One effective-dated charge schedule shared with sizing. Tag quote-only vs depth-informed fills. No fill when the quote is stale.

**Success criteria:**

- Sizer charge and fill charge for the same order match the schedule version.
- A stale book produces no invented close.
- Partial depth fills only the displayed quantity.
- PAPER adapter tests still cannot select a live transaction endpoint.

### 2.11 Live broker adapter

**Path:** `src/trading/broker/fyers/adapter.py`  
**Required change:** No new live order path. Mapping stays available for reconciliation research. This redesign does not call it for entries.

**Success criteria:**

- Isolation check passes.
- No test or session config sets environment to live execution.

### 2.12 Position lifecycle and trade manager

**Path:** `src/trading/trade/manager.py`, `src/trading/domain/state/machines.py`, `src/trading/domain/contracts/position.py`  
**Required change:** Persist owner mode, family id, policy version, campaign id, and frozen exit policy. Keep `REPAIR_REQUIRED`. Add states only when a real transition needs them. One management lease per position.

**Success criteria:**

- Two managers cannot both submit an exit for the same position.
- A new strategy version does not rewrite the exit policy of an open trade.
- Roll close/open keeps campaign drawdown. A new position id does not zero it.
- Recovery order is: config, restore, reconcile, unknowns, exits, then entries.

### 2.13 Exits and valuation

**Path:** `src/trading/trade/exits.py`, `src/trading/trade/sentinel.py`  
**Required change:** Spreads stop and mark on conservative whole-structure value (all legs). Single-leg positions may keep leg-price stops. Legacy open trades keep the policy stored at entry. Gap exits use the executable price. Software stops stay labelled software stops.

**Success criteria:**

- A test where the long leg is inside its tick stop and the spread value is through the structure stop exits on the structure value.
- Tighten never loosens a stop.
- A missed intra-poll print is reported as `LIMITATION_CONFIRMED`, not as protection success.
- M1 targets are config and do not force a winner smaller than the mode’s stated payoff shape.

### 2.14 Reviews

**Path:** `src/trading/trade/review.py`, `src/trading/runtime/review_schedule.py`  
**Required change:** M3/M4 and carried M2 positions review at 10:30 and 14:30. M1 uses its time/event exit, not these slots. If both slots were missed, run one current recovery and record the missed ids. ROLL and SWITCH submit only for families that already have a safe close and open. Otherwise store `PROPOSED_NOT_EXECUTED`.

**Success criteria:**

- Duplicate delivery of one slot executes one action.
- Two missed slots do not execute two stale switches.
- HOLD writes a reason and the next slot time.
- HEDGE is funded by the owning mode and cannot create an unbounded short.

### 2.15 Carry

**Path:** `src/trading/trade/eod_scanner.py`  
**Required change:** Replace the profit-and-conviction carry function for M2. Carry approves only with a fresh thesis, remaining expiry, overnight loss budget, event check, portfolio approval, persisted exit state, and next-session recovery health. A loss does not get a looser rule. A profit alone does not approve.

**Success criteria:**

- Tests cover `CARRY_APPROVED` and `CARRY_REJECTED`.
- Rejected carry starts an exit while the session is still tradable.
- The position’s mode stays M2.
- The old “must be in profit” rule is not on this path.

### 2.16 Protection coordinator

**Path:** `src/trading/runtime/protection.py`  
**Required change:** Keep fast monitoring of open positions. Do not describe it as M1 entry. Degraded quotes alert and freeze new risk. They do not invent a fill.

**Success criteria:**

- Entry age and protection age are separate metrics.
- Agent timeout does not stop an exit.
- Heartbeat loss is visible to the watchdog.

### 2.17 Safety controls and paper data gate

**Path:** `src/trading/safety/controls.py`, `paper_data.py`, `readiness.py`  
**Required change:** Halt and daily-loss freeze can target one mode without freezing the others’ exits. Global daily-loss breach freezes all new risk. P0 data failure still blocks new exposure.

**Success criteria:**

- Mode daily-loss breach blocks that mode’s entries and still allows its exits.
- Global breach blocks every new entry.
- Unknown order freeze survives restart.

### 2.18 Trading store

**Path:** `src/trading/storage/trading_store.py`  
**Required change:** Schema version for mode owner, campaign, reservation, and freeze. Migrations are idempotent. Rollback must not reopen a closed trade or drop a new fill.

**Success criteria:**

- A migration test round-trips an open position’s owner, policy version, and freeze.
- Duplicate `decision_id` / idempotency key fails closed.

### 2.19 Paper session and runner

**Path:** `src/trading/runtime/paper_session.py`, `paper_runner.py`  
**Required change:** Replace the long/debit winner branch and the CAS special case with four mode producers and the arbiter. Equity comes from the four ledgers. CAS entry frequency follows the G3 decision. M3/M4 may remain on a slower poll.

**Success criteria:**

- One cycle can emit candidates from more than one mode.
- Only arbiter-approved intents reach the gateway.
- Flat modes write a reason and the next evaluation time.
- Existing PAPER stances are unchanged until that family’s G2 evidence exists.

### 2.20 Cycle evidence, cohort, event risk

**Path:** `src/trading/runtime/cycle_evidence.py`, `cohort.py`, `event_risk.py`  
**Required change:** Record the funnel `evaluation → signal → bound contracts → structure → mode risk → portfolio → order → fill → exit`, including abstentions. Event blackout remains deterministic.

**Success criteria:**

- Each required drop reason can appear in the funnel, including `MIN_LOT_EXCEEDS_BUDGET`.
- Reports show all scheduled market time and healthy time separately.
- A blackout blocks new risk even if a macro score is bullish.

---

## Layer 3 — four mode engines

Strategies stay pure: `FeatureSnapshot` in, `TradeIntent` out, no quantity, no broker, no LLM.

### 3.1 Mode policy and intent contract

**Path:** `src/trading/domain/enums.py` (`ModeId`), `src/trading/domain/contracts/intent.py`, new mode policy model, `config/modes.yaml`  
**Required change:** Put `mode_id` and `family_id` on the intent without adding quantity or a broker field. One mode file lists allowed families, fractions, windows, expiry rules, and review cadence. Startup rejects a PAPER family with no binder, payoff check, OMS plan, and G2 record.

**Success criteria:**

- Unknown family or mode fails validation.
- Intent tests still forbid a quantity field.
- `config/modes.yaml` is the only mode-policy source. Limits are not copied into a second conflicting file.
- `StructureChoice` in `domain/contracts/advice.py` either maps onto the new family ids or is retired from the execution path.

### 3.2 Router and allow-table

**Path:** `src/trading/identification/router.py`, `allow_table.py`, `config/identification.yaml`  
**Required change:** Stop choosing one paper winner between long option and debit spread. Allow-table becomes per-mode family eligibility. Range structures do not require an UP/DOWN trend.

**Success criteria:**

- A RANGE regime can produce an iron-condor or butterfly candidate when that family is in M4’s allowlist and G1 fits.
- High IV is not the only way to reach a credit vertical.
- The session no longer calls `route_nifty_options` as the execution authority.

### 3.3 Binders

**Path:** `src/trading/identification/binders.py`  
**Required change:** Split binders by mode. M2: following listed week, exclude 0/1 calendar DTE, delta band about 0.45–0.65. M1: own moderately OTM band, 0-DTE off by default, not the M2 binder. M3: four verticals, no single leg. M4: its basket, including range and volatility shapes.

**Success criteria:**

- M2 on a 0-DTE or 1-DTE chain abstains or selects the next eligible listed expiry, with the calendar reason stored.
- M1 and M2 can select different strikes on the same chain.
- No binder emits a symbol that is absent from the instrument master.
- Holiday or monthly substitution is an explicit branch with a test.

### 3.4 Regime and market state

**Path:** `src/trading/identification/regime.py`, `market_state.py`, `p1_features.py`, `macro.py`, `vix.py`  
**Required change:** Keep them as feature inputs. Mode policies read them. They do not veto an entire mode because trend is not UP or DOWN.

**Success criteria:**

- A range market state still reaches M4 candidate generation.
- A directional market state still reaches M2 candidate generation.
- Missing VIX fails a VIX-gated rule and does not default to zero.

### 3.5 M1 long call / long put

**Path:** `src/trading/strategies/cas_microstructure.py` (retarget; do not fork a second CAS class)  
**Required change:** After G3, select inside the M1 band using scenario payoff, liquidity, and one-lot cost. Episode id, cooldown, and daily loss cap. Quote-only and depth-only profiles stay distinct. 0-DTE entry default off.

**Success criteria:**

- The cheapest strike does not win when a moderately OTM liquid strike scores better.
- The same signal episode does not create a second order before reset.
- Daily loss cap blocks new M1 risk across restart.
- Status stays below `PAPER_STANCE_ENABLED` until G3’s path and the G2 lifecycle set both pass.
- The strategy module contains no broker call and no LLM call.

### 3.6 M2 long call / long put

**Path:** `src/trading/strategies/long_option.py`  
**Required change:** New entries are M2 single-leg only. Expiry and carry rules come from the binder and the carry gate. Do not relabel a carry as M3. Do not register `directional_conviction.py`.

**Success criteria:**

- New M2 intents have `mode_id=M2_DIRECTIONAL` and one long option leg.
- Open legacy `positional_long_option` positions keep their stored exit ticks until closed.
- `build_strategy("directional_conviction")` is not on the paper import path.
- G2 lifecycle evidence exists before any stance change.

### 3.7 M3 debit verticals

**Path:** `src/trading/strategies/debit_spread.py`  
**Required change:** Tag bull call and bear put as M3 families. Compare executable debit, scenario P&L, max loss, and liquidity. Thesis horizon is mandatory. Unlimited hold because time exit is null is rejected.

**Success criteria:**

- Each debit family reaches gateway, fill, management, and close in a scenario test before PAPER.
- An M3 intent with one leg is rejected.
- Whole-structure valuation, not the long-leg 40/80 tick stop, drives new M3 risk.

### 3.8 M3 credit verticals

**Path:** `src/trading/strategies/multileg_options.py`  
**Required change:** Split `defined_risk_multileg` into `bull_put_credit` and `bear_call_credit`. Binder plus protection-before-short plus repair. Session currently gives this id an empty candidate list. That must become a real route or stay SHADOW with the gap visible.

**Success criteria:**

- Both families have G2 evidence before PAPER.
- A partial fill never reports the spread `OPEN` while a short is uncovered.
- The old catch-all id does not remain an executable alias.

### 3.9 M4 iron condor

**Path:** `src/trading/strategies/iron_condor.py`  
**Required change:** Binder, payoff check including asymmetric wings, prefix-safe order (longs before shorts), then the G2 set. PAPER only if G1 `one_lot_fits` is true for that family.

**Success criteria:**

- Wider-wing loss formula matches the generic kink/slope check.
- Short-first approval is gone for this family.
- The family is absent from `strategy_ids` or remains SHADOW until G2 and G1 both pass.
- A strategy spec file exists when the family is claimable.

### 3.10 M4 butterflies, straddle, strangle, iron butterfly

**Path:** new strategy modules, one family per slice  
**Required change:** Legs, payoff, sizer, safe prefixes, and G2 evidence per family. Long straddle and strangle are debit-only. A 1:2:1 butterfly is allowed when the payoff tails are finite. Range path must not need UP/DOWN.

**Success criteria:**

- Each family has its own lifecycle tests.
- Turning one family to PAPER does not turn the others to PAPER.
- Straddle and strangle stay non-executable while G1 says they exceed the M4 cap.
- No short straddle or short strangle type exists.

### 3.11 M4 calendars

**Path:** none today (`MacroCalendar` is not an option structure)  
**Required change:** Research registry and a dual-expiry settlement ledger. Do not use same-expiry vertical formulas. Do not admit them to the strict ₹7L book until a finite bound is demonstrated for missed near-expiry exit and a later reversal.

**Success criteria:**

- Status is `EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN` until that proof exists.
- A test shows the same-expiry loss formula is refused for a calendar.
- No PAPER stance on the strict book.

### 3.12 Commodity futures

**Path:** `src/trading/strategies/commodity_futures.py`  
**Required change:** Keep the code. Keep stance SHADOW. Gateway rejects it as an execution underlying on the NIFTY deployment.

**Success criteria:**

- Session stance remains SHADOW unless an operator changes it, and a NIFTY-only gateway reject still blocks a PAPER flip.
- Spec `04_COMMODITY_FUTURES.md` is labelled out of this deployment, not deleted.

### 3.13 Strategy registry

**Path:** `src/trading/strategies/__init__.py`, `base.py`  
**Required change:** Register a family in the same change that adds its binder and tests. Strategies stay pure.

**Success criteria:**

- Importing the package does not register an unwired family as executable.
- A purity test still forbids broker, OMS, and network imports from strategy modules.

---

## Layer 4 — analytics, agents, operator view

Layer 4 may propose. It may not size, stop, submit, or edit live limits. Do not rebuild Agent Desk.

### 4.1 Scorecard, eligibility, judgment

**Path:** `src/trading/analytics/scorecard.py`, `eligibility.py`, `judgment.py`  
**Required change:** Cohort by mode, family, policy version, capability profile, and executable vs counterfactual. Separate “followed policy” from “made money.”

**Success criteria:**

- A losing policy-compliant trade is not labelled `LOSS_SHOULD_HAVE_PASSED`.
- A rejected candidate is not counted as a realized loss.
- Executable P&L and counterfactual P&L are never added together.

### 4.2 Fill reconstruction

**Path:** `src/trading/analytics/fills.py`  
**Required change:** Remain the conservative bid/ask model used by the paper broker. Share the charge schedule with Layer 2.

**Success criteria:**

- Same fixture, same schedule version, same fill in sizer cost and in the scorecard.
- MFE is not reported as the exit price.

### 4.3 Counterfactual book

**Path:** `src/trading/analytics/counterfactual.py`  
**Required change:** Independent M3/M4 hypothetical books may overlap. They must not reserve cash or change executable P&L.

**Success criteria:**

- A test runs a counterfactual evaluation and asserts reservation and position tables are unchanged.

### 4.4 Bias, improvements, research loop

**Path:** `src/trading/analytics/bias.py`, `improvements.py`, `research_weekly.py`, `hypothesis_promotion.py`  
**Required change:** Proposals may adjust thresholds inside predeclared ranges. They cannot raise max-loss fractions or bypass hard checks. Hypotheses enter the existing SHADOW ladder only.

**Success criteria:**

- A proposal that raises a mode’s per-trade cap is rejected by the validator.
- No proposal writes `config/modes.yaml` or `config/risk.yaml` by itself.
- Agent remains disabled in `config/agent.yaml` until an operator enables it.

### 4.5 Agent desks

**Path:** `src/trading/ai/` (`advise.py`, `entry.py`, `position.py`, `macro.py`, `portfolio_desk.py`, and the rest)  
**Required change:** When a review needs a ranking, pass deterministic candidate ids. Validate the reply. Timeout uses the deterministic action. Update advise’s allowed structure list only after family ids exist. No new framework.

**Success criteria:**

- Malformed, stale, or out-of-policy model output does not submit an order.
- An exit trigger does not wait on the model.
- Tools cannot place orders, read secrets, or run a shell.
- Prompt text in a news field does not widen tool permissions.

### 4.6 Authority and budget

**Path:** `src/trading/ai/authority.py`, `budget.py`  
**Required change:** Keep C1: BOUNDED is config-promotion only. Intraday path stays LLM-free.

**Success criteria:**

- A test shows BOUNDED cannot VETO or size a live intent.
- Budget exhaustion abstains and does not block an exit.

### 4.7 Paper readiness

**Path:** `src/trading/analytics/paper_readiness.py`  
**Required change:** Readiness for a family requires `LIFECYCLE_PROVEN` plus G1 `one_lot_fits` where a lot is required. Unit tests do not flip the flag.

**Success criteria:**

- Iron condor with only a sizer test is not readiness-ready.
- Calendars cannot be readiness-ready on the strict book while the bound is unproven.

### 4.8 Dashboard and CLI

**Path:** `src/trading/dashboard/`, `src/trading/cli.py`  
**Required change:** Show four mode allocations, positions, owner, risk used, data age, next review, recent decisions, incidents, and PAPER isolation. Keep `trading risk one-lot` as a report command. Do not add a command that sets a family to PAPER.

**Success criteria:**

- Operator view matches the ledgers for a fixture session.
- CLI help names NIFTY paper scope and does not offer live order placement for this redesign.

### 4.9 Stress and invalidation

**Path:** `src/trading/analytics/stress.py`, `invalidation.py`  
**Required change:** Stress scenarios include gap, IV shock, and one leg unavailable. Invalidation can trigger review. It cannot bypass Layer 2.

**Success criteria:**

- A tail breach freezes new entries and leaves exits on.
- Invalid pricing is a failure, not zero risk.

---

## Cross-cutting

### 5.1 Configuration set

**Path:** `config/paper.yaml`, `risk.yaml`, `paper_session.yaml`, `identification.yaml`, `evaluation.yaml`, `paper_data.yaml`, `agent.yaml`  
**Required change:** Add `config/modes.yaml` only. Do not duplicate account limits. Startup cross-checks mode, family, sizer, and stance.

**Success criteria:**

- PAPER stance for an unknown family fails process start.
- Commodity and BANKNIFTY are not execution underlyings in the NIFTY session config.

### 5.2 Operations

**Path:** `src/trading/ops/`, `src/trading/runtime/watchdog.py`, `session_heartbeat.py`, `deploy/`  
**Required change:** No redesign of the watchdog in the mode slices. Runbook updates when stances actually change. Restart still reconciles before entries.

**Success criteria:**

- A killed paper process is detected by the heartbeat outside the trading process.
- Restart does not submit a second entry for an UNKNOWN order.
- Ops units are not required to import mode policy to keep exits running.

### 5.3 Canonical docs

**Path:** `docs/context/ARCHITECTURE.md`, `CURRENT_STATE.md`, `STRATEGY_SPECS/`, `docs/plans/ACTIVE_PLAN.md`  
**Required change:** Update each document in the slice that changes that behavior. ARCHITECTURE’s five-strategy list becomes four NIFTY modes plus disabled commodity. Do not mark `LIVE_APPROVED`.

**Success criteria:**

- `CURRENT_STATE.md` distinguishes `IMPLEMENTED_UNIT`, `LIFECYCLE_PROVEN`, and `PAPER_STANCE_ENABLED` per family.
- No doc says all option strategies are active while calendars or straddles are blocked.
- “Naked” is not used for a long option.

---

## What is already sufficient and should not be rewritten

| Item | Why it stays |
| --- | --- |
| `TradeIntent` has no quantity or broker id | Layer 3 must keep emitting intents only |
| Distinct leg snapshot ids | `snapshot_bundle.py` already rejects id erasure |
| UNKNOWN order freeze and idempotency | OMS already does this for the current path |
| Bid/ask paper fills | Fill model is already conservative |
| No LLM on the order path | Agent Desk C1 |
| 10:30 and 14:30 review slots | Extend missed-slot behavior; do not replace the clock |
| Open-position protection poll | Keep it; do not call it M1 entry |
| Commodity code | Disable on this deployment; do not delete |

## What must not be claimed yet

- Four mode engines are running.
- G1 `one_lot_fits` authorizes PAPER.
- M2 can trade by moving to delta 0.41.
- Credit spreads or iron condors are session-integrated.
- Calendars have a bounded loss.
- The 60-second loop is microstructure execution.
