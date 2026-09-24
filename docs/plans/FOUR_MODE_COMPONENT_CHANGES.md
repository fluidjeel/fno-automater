# Four-mode component changes

**Inspected:** 24 September 2026, `main` @ `47dcf15` (dirty tree; ops edits preserved)  
**Target:** [NIFTY_FOUR_MODE_CURSOR_REDESIGN.md](NIFTY_FOUR_MODE_CURSOR_REDESIGN.md) v1.1  
**Execution order:** [FOUR_MODE_REDESIGN_PLAN.md](FOUR_MODE_REDESIGN_PLAN.md)

Verdicts: `KEEP`, `EXTEND`, `OVERHAUL`, `NEW`, `DISABLE`.  
G2 statuses name the promotion ladder in spec §31.4. Legacy `PAPER` in `config/paper_session.yaml` is current session configuration. It is not a G2 certificate for the redesigned policy.

---

## 1. Domain contracts

**Verdict:** EXTEND  
**Phase:** P1  
**G2:** n/a (types)

**Current.** `StructureChoice` in `src/trading/domain/contracts/advice.py` lists `positional_long_option`, `debit_spread`, `defined_risk_multileg`, `cas_microstructure`, `commodity_futures_trend`, `PASS`. `StructureKind` in `identification.py` has long option, debit, credit, CAS, commodity future, directional conviction. `HoldingStyle` is `POSITIONAL` or `INTRADAY`. `TradeState` is `PENDING_ENTRY`, `OPENING`, `OPEN`, `EXIT_PENDING`, `CLOSING`, `CLOSED`, `REPAIR_REQUIRED`. `ReviewAction` includes `HOLD`, `TIGHTEN_STOP`, `PARTIAL_EXIT`, `FULL_EXIT`, `PROPOSE_HEDGE`, `PROPOSE_ROLL`. Hedge and roll are proposals and do not auto-submit. `ExitScope` already has `SPREAD_VALUE`. `DataQuality` is `VALID`, `DEGRADED`, `STALE`, `INVALID`.

**Gap.** R-002, R-024. No mode policy, decision bundle, per-mode position owner, or campaign id. Lifecycle names in R-019 (`CANDIDATE`, `RESERVED`, `DEGRADED`, `SETTLEMENT_PENDING`, and others) are richer than `TradeState`. Unknown strategy fields must keep failing closed.

**Proposed change.** Add a `ModeId` (`M1_CAS`, `M2_DIRECTIONAL`, `M3_TACTICAL_POSITIONAL`, `M4_STRATEGIC_POSITIONAL`) and a frozen `ModePolicy` that points at allowed family ids, risk fractions, expiry rules, and review cadence. Extend `TradeIntent` lineage with `mode_id` and `family_id` without adding a broker command or a final quantity. Map new lifecycle names onto the existing machine where the meaning already exists (`REPAIR_REQUIRED` stays). Add states only when a transition is real. Add `MISSING` and `INCONSISTENT` quality states when a producer can emit them. Leave `PROPOSE_HEDGE` / `PROPOSE_ROLL` as non-submitting until P13 has a safe plan.

**Tests.** Strict round-trip; unknown family rejected; intent still has no quantity field (`tests/test_l2_contracts.py` pattern).

---

## 2. Configuration

**Verdict:** EXTEND  
**Phase:** P1, values gated by G1  
**G2:** n/a

**Current.**

| File | Role today |
| --- | --- |
| `config/paper.yaml` | PAPER account, session 09:15–15:30, `max_loss_per_trade_fraction` 0.01, daily loss 0.03, portfolio risk 0.10 |
| `config/risk.yaml` | Policy version 6. Strategy fractions: long option 0.20, POC 0.20, commodity 0.20, debit 0.05, multileg 0.05, CAS 0.05, directional conviction 0.15. Charges ₹50/lot. Tail budget 0.08 |
| `config/paper_session.yaml` | Poll 60s, EOD 15:40, strikes each side 2, stances below, reviews 10:30 and 14:30 |
| `config/identification.yaml` | Shared delta and DTE bands, one router, CAS window 15:00–15:30 |
| `config/evaluation.yaml` | Conservative fill model |
| `config/paper_data.yaml` | P0/P1 paper data contract |
| `config/agent.yaml` | Agent disabled |
| `config/directional_conviction.yaml` | Unused by the paper session id list |

Session stances: `positional_long_option` PAPER, `debit_spread` PAPER, `defined_risk_multileg` SHADOW, `cas_microstructure` PAPER, `commodity_futures_trend` SHADOW.

**Gap.** R-027. Ten new YAML files would duplicate these sources. Strategy constants (`REQUESTED_RISK_INR = 10000`, tick stops) still live in strategy modules.

**Proposed change.** One new `config/modes.yaml` for mandate, family allowlist, windows, expiry rules, and the §10.1 fractions. Capital math continues to read `config/risk.yaml` and `config/paper.yaml` until P2 moves the mode ledgers behind the existing risk loader. Startup validation rejects a family that is PAPER without a binder, payoff check, OMS plan, and G2 record. Move tick stops and requested-risk literals into config when the owning slice touches that strategy. Do not copy charges into a third file; reconcile ₹50 sizing vs the fill-model charge in the fills component.

---

## 3. Calendar and session clocks

**Verdict:** EXTEND  
**Phase:** P5 for expiry selection; clock values stay until external verification  
**G2:** n/a

**Current.** Continuous window 09:15–15:30 and auction 15:30–15:40 in `config/identification.yaml`. Paper process EOD 15:40. Daemon market close 15:30. Review slots 10:30 and 14:30 in `config/paper_session.yaml`, due logic in `src/trading/runtime/review_schedule.py`. No single calendar service that stores holiday source, effective date, special sessions, and flatten cutoff together. Expiry identity comes from the chain the pipeline loaded.

**Gap.** R-008, R-010, T16, T17, T51. Spec sources S1/S2 still need an effective circular. Do not write 15:40 into every segment because a webpage lists it.

**Proposed change.** Add a calendar port that, given an injected clock and the instrument master, returns session date, phase, listed expiries, and entry/flatten deadlines. M2 following-week selection uses that port (P5). Persist source and effective date on the config record. Leave 15:30 and 15:40 as they are until the verification note is filled in.

---

## 4. Universe and NIFTY-only execution

**Verdict:** EXTEND  
**Phase:** P1  
**G2:** n/a

**Current.** The identification route is NIFTY-centric. BANKNIFTY appears in data helpers and CLI chain recording, not as a paper strategy route. Commodity CRUDEOIL/MCX is a SHADOW strategy on the same session process. Gateway structure detection still understands commodity futures because that strategy exists.

**Gap.** R-001, T01, T55. A non-NIFTY intent is not rejected solely because the router is NIFTY-shaped. PAPER isolation from a live transaction endpoint is a separate existing check (`trading paper isolate` path in the CLI).

**Proposed change.** Gateway rejects an execution intent whose underlying is outside the approved NIFTY option set, with a stable reason code. Data ingestion of futures or index series remains allowed as signals. Commodity evaluation can stay loaded only while stance is SHADOW and the NIFTY route cannot select it.

---

## 5. Snapshots, provenance, and data quality

**Verdict:** KEEP the per-leg ID rule; EXTEND quality states  
**Phase:** P3 records the bundle on the decision; quality states with the first consumer that needs them  
**G2:** n/a

**Current.** `validate_leg_snapshot_bundle` in `src/trading/risk/snapshot_bundle.py` requires each leg’s own snapshot, checks instrument identity, freshness, and cross-leg skew, and rejects ID erasure. The module docstring states that copying the parent snapshot id onto every leg is a rejected design. `DataQuality.blocks_new_exposure` is true for `STALE` and `INVALID`. `DEGRADED` does not block. There is no `MISSING` or `INCONSISTENT` enum value.

**Gap.** R-012 is largely met for the ID-overwrite failure mode. R-007’s capability profile and §7.2’s extra states are not. T08 should lock the current distinct-ID behavior so a later edit cannot collapse ids.

**Proposed change.** Keep `snapshot_bundle.py`. Add a regression test that two different leg snapshot ids in one decision cycle pass, and that a stale leg fails. Introduce `MISSING` and `INCONSISTENT` only with a producer. Mandatory-input failure blocks new exposure. Open positions with a bad quote enter the existing degraded-protection path (`ProtectionCoordinator`) and must not invent a close.

---

## 6. Features, IV, and Greeks

**Verdict:** KEEP  
**Phase:** consumed by P3–P11; no standalone rewrite  
**G2:** n/a

**Current.** Layer 1 owns bars, IV, and Greeks. Strategies read `FeatureSnapshot`. Identification warmup is 50 completed bars and 20 sessions (`config/identification.yaml`). Delta bands for binding are configured, not hardcoded in the binder arithmetic itself.

**Gap.** R-014 scenario repricing for CAS (underlying move, IV up/down/flat, time passage, executable bid/ask) does not exist as a selector. IV percentile is used by the router as a structure preference, which §2 retires as the global long-vs-debit switch.

**Proposed change.** Leave the calculators. Mode selectors call them. A missing delta fails a delta gate. It is never treated as zero. CAS scenario scores stay scenario scores until a later calibration slice has frozen forward outcomes.

---

## 7. Contract binders

**Verdict:** OVERHAUL  
**Phase:** P4–P7, P10–P11  
**G2:** binders alone are `IMPLEMENTED_UNIT`

**Current.** `src/trading/identification/binders.py` binds a long option and a debit spread. Weekly DTE 5–12 or monthly 20–35. Long absolute delta 0.45–0.60. Debit short delta 0.20–0.35. Minimum reward/risk 1.50. Chain width is ATM ± `option_strikes_each_side` (2) from `src/trading/runtime/candidates.py`. CAS reuses `long_binding.candidates`. No binder for credit verticals, condors, butterflies, straddles, strangles, or calendars.

**Gap.** R-010, R-014, T10, T16, T20. M1 wants roughly 0.15–0.35 absolute delta as a research default. M2 wants a following-week listed expiry and a 0/1 DTE exclusion. M3 must not receive a single leg.

**Proposed change.**

| Producer | Binder |
| --- | --- |
| M2 | Existing long binder, then a following-week expiry filter (P5) |
| M3 debit | Existing debit binder, tagged M3, single-leg rejected (P4) |
| M3 credit | New same-expiry credit binder (P6) |
| M1 | New long-option set, own delta band, own expiries, 0-DTE off unless a separate experiment flag is on (P7, after G3) |
| M4 | New binders per family as each family slice lands |

Abstain with a reason when the chain has no eligible contract. Do not synthesize a symbol from a weekday.

---

## 8. Router and allow-table

**Verdict:** OVERHAUL  
**Phase:** P4  
**G2:** n/a

**Current.** `route_nifty_options` prefers debit when realized vol is expanding, long option when IV percentile and IV/RV are low, otherwise debit. One `paper_winner`. Others are shadows. Cooldown 30 minutes. Allow-table in `config/identification.yaml` drops multileg unless HIGH IV and RANGE, and drops CAS outside its window. Any open NIFTY position feeds a correlation block in the session builder. `paper_session.py` then special-cases CAS execution outside the winner.

**Gap.** R-005, R-016, T22, T27. One winner cannot represent four mandates. An UP/DOWN rule makes range structures unreachable. Correlation-on-any-open-position cannot express “M1 and M2 may both be long if incremental risk fits.”

**Proposed change.** Delete the winner preference as the execution authority in P4. Each mode producer emits candidates. A first arbiter suppresses exact duplicates (same normalized legs, side, ratio, expiry, episode) and records the incumbent id. Economic overlap, conflict, and the two-position M4 cap wait for P12. Counterfactual rows append to an evaluation log and do not call the reservation store. Until P4, the current router remains the live paper path.

---

## 9. Strategy registry

**Verdict:** EXTEND  
**Phase:** each family slice  
**G2:** see family rows

**Current.** `src/trading/strategies/__init__.py` imports and therefore registers long option, debit, multileg, CAS, commodity, iron condor. It does not import `directional_conviction.py`, so `build_strategy("directional_conviction")` fails on the normal path. Every concrete strategy requests `Decimal("10000")` risk and sets tick stops in the module. Strategies return `TradeIntent` only.

**Proposed change.** Keep the registry and the purity rule. Register a family only in the slice that also lands its binder and tests. Map legacy ids:

| Legacy id | New family | Mode |
| --- | --- | --- |
| `cas_microstructure` | `long_call` / `long_put` under M1 policy | M1 |
| `positional_long_option` | `long_call` / `long_put` under M2 policy for new entries | M2 |
| `directional_conviction` | Retired as a second plugin. Useful checks fold into the M2 policy | M2 |
| `debit_spread` | `bull_call_debit` / `bear_put_debit` | M3, later M4 |
| `defined_risk_multileg` | `bull_put_credit` / `bear_call_credit` | M3, later M4 |
| `iron_condor` | `short_iron_condor_defined` | M4 |
| `commodity_futures_trend` | Unchanged id, deployment disabled | none |

Open trades keep `strategy_id` and exit policy as stored. New ids apply to new entries.

---

## 10. Family: long call / long put (M2)

**Verdict:** EXTEND  
**Files:** `src/trading/strategies/long_option.py`, `src/trading/risk/sizing/long_option.py`, spec `docs/context/STRATEGY_SPECS/02_POSITIONAL_LONG_OPTION.md`  
**Phase:** P4, P5, P8, P9  
**G2:** legacy session stance PAPER. Redesigned M2 policy is not `LIFECYCLE_PROVEN`.

**Current.** Min DTE 7, stop 40 ticks, target 80 ticks, `LEG_PRICE` scope, holding style positional when the runner says so. Binder delta 0.45–0.60 matches the M2 research band (0.45–0.65) closely enough to extend rather than replace. Sizer takes the minimum of risk, capital, margin, portfolio, and liquidity lots.

**Gap.** Following-week expiry and 0/1 DTE exclusion are absent. Carry is the EOD scanner (component 20), not §4.2.

**Proposed change.** Keep the strategy pure. P5 adds the expiry filter in the binder. P8 leaves leg-price stops for true single-leg positions. P9 replaces carry. Re-run entry, partial fill, monitor, exit, and restart before any stance change. Update spec 02 when P5 lands, not in this doc slice.

---

## 11. Family: CAS long option (M1)

**Verdict:** OVERHAUL the selector and the entry clock; KEEP the feature pipeline  
**Files:** `src/trading/strategies/cas_microstructure.py`, `src/trading/data/cas_features.py`, `src/trading/data/cas_depth/`, spec `05_CAS_MICROSTRUCTURE.md`  
**Phase:** G3, then P7  
**G2:** legacy stance PAPER inside 15:00–15:30. Redesigned M1 is blocked on G3.

**Current.** Strategy window 15:00–15:30 IST weekdays. Min DTE 1. Stop 20 ticks, target 30 ticks, holding 900 seconds, `LEG_PRICE`. It consumes `cas-microstructure-v1` keys and fails closed on a data gap. `cas_paper_execute` submits only when stance is PAPER, the allow-table includes CAS, and exactly one long-binding candidate exists. That candidate uses the 0.45–0.60 delta band. Spec 05 calls the hypothesis close-auction microstructure and allows PAPER when P1 depth is observed. Fyers metadata: no aggressor side, index depth unsupported, TBT depth is an entitled NSE/NFO add-on (`capability_probe.py`). Entry poll is 60 seconds.

**Gap.** R-009, R-014, T10–T15. The name CAS collides with NSE’s cash closing auction. Strike choice is the long binder. A 30-tick target plus a 60-second decision loop fights the low-win-rate, short-horizon mandate. 0-DTE is not an explicit off switch; min DTE 1 still allows same-day expiry.

**Proposed change.** G3 writes a field inventory and a latency measurement and stops. P7, only if G3 allows a path, adds an M1 candidate set (research delta about 0.15–0.35), episode ids, cooldown, quote-only vs depth-only labels, and 0-DTE entry default off. Feature code stays. Spec 05 is rewritten in the P7 doc update so “PAPER” describes the certified path only.

---

## 12. Family: debit verticals (M3)

**Verdict:** EXTEND  
**Files:** `src/trading/strategies/debit_spread.py`, `src/trading/risk/sizing/debit_spread.py`, spec `03_DEBIT_SPREAD.md`  
**Phase:** P3, P4, P8  
**G2:** legacy stance PAPER. Redesigned M3 debit is not `LIFECYCLE_PROVEN` until P4 includes the G2 scenarios.

**Current.** Bull call / bear put geometry, min reward/risk 1.50, min DTE 7, stop/target 40/80 on the long leg (`LEG_PRICE`). Gateway orders long then short. Partial-fill policy on the intent is all-or-cancel. `REPAIR_REQUIRED` blocks new entries.

**Gap.** R-003’s mode role, T21, T42. Long-leg ticks are not whole-structure P&L. The family is the router’s alternative to a long option, not a tactical-positional mandate with a thesis horizon.

**Proposed change.** P3 checks debit loss = net debit × lot × structure lots against the generic kink/slope payoff. P4 tags new debits as M3 and rejects an M3 single-leg. P8 moves spread risk to `SPREAD_VALUE` or strategy P&L built from all legs. Existing open debits keep 40/80 until closed.

---

## 13. Family: credit verticals (M3)

**Verdict:** EXTEND, then prove lifecycle  
**Files:** `src/trading/strategies/multileg_options.py`, `src/trading/risk/sizing/credit_spread.py`, spec `06_DEFINED_RISK_MULTILEG.md`  
**Phase:** P6  
**G2:** `LIFECYCLE_PROVEN` (P6). Session stance SHADOW until explicitly wired in `strategy_ids`.

**Implemented (P6).** `BullPutCreditStrategy` / `BearCallCreditStrategy` with `bull_put_credit` / `bear_call_credit` family ids; `bind_credit_spread`; gateway `_approved_credit_spread_legs` returns long protection then short; G2 lifecycle proven; partial fill routes to `REPAIR_REQUIRED`; `defined_risk_multileg` catch-all blocked from PAPER. Locked in `tests/test_p6_credit_verticals_and_g2.py`.

**Remaining gap.** Not session-routed in `config/paper_session.yaml` `strategy_ids` (same as other G2-proven families awaiting explicit wiring).

---

## 14. Family: short iron condor (M4)

**Verdict:** EXTEND  
**Files:** `src/trading/strategies/iron_condor.py`, `src/trading/risk/sizing/iron_condor.py`  
**Phase:** P10, after G1  
**G2:** `LIFECYCLE_PROVEN` (P10). Not in `strategy_ids`. No strategy spec file.

**Implemented (P10).** `bind_iron_condor` (RANGE-only); `formula_short_iron_condor_defined` with `max(put_width, call_width)`; gateway `_approved_condor_legs` emits long put, short put, long call, short call; `IronCondorStrategy` stamps `ModeId.M4_STRATEGIC_POSITIONAL` and `FamilyId.short_iron_condor_defined`; G2 lifecycle proven; `short_iron_condor_defined` removed from `G2_UNPROVEN_FAMILIES`; legacy `iron_condor` strategy id stays G2-blocked. Locked in `tests/test_p10_iron_condor_binder_and_g2.py`. G1 row for `short_iron_condor_defined` is `AFFORDABLE` (`docs/reports/G1_ONE_LOT_AFFORDABILITY.md`).

**Remaining gap.** No strategy spec file yet. Not session-routed in `config/paper_session.yaml` `strategy_ids`.

---

## 15. Families: butterfly, straddle, strangle, iron butterfly

**Verdict:** NEW  
**Phase:** P11, one family at a time  
**G2:** `ABSENT` until that family’s slice. Each gets its own `LIFECYCLE_PROVEN` before PAPER.

**Current.** No strategy, binder, sizer, or spec.

**Gap.** R-011, T05, T22, T23. A 1:2:1 long butterfly is allowed by the spec because the payoff is bounded, not because leg counts match. Range structures must be reachable without an UP/DOWN trend.

**Proposed change.** Per family: legs, payoff formula, generic tail check, sizer, protection order, and the five G2 scenarios. Long straddle and strangle are debit-only (no short entry). Short iron butterfly uses the same prefix rule as the condor. Do not enable the four families in one stance change.

---

## 16. Families: long call / put calendars

**Verdict:** NEW, experimental  
**Phase:** P14  
**G2:** `EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN`. Strict-book PAPER is forbidden until a dual-expiry bound exists.

**Current.** Absent. `MacroCalendar` is a news/event object, not an option structure.

**Gap.** §5.1, T06. Initial debit is not a proof of bounded loss across two NIFTY settlements.

**Proposed change.** Research registry, binder, and a settlement ledger that refuses the same-expiry formulas. Report the unsupported path (missed near-expiry exit, then a far-leg reversal). No reservation on the ₹7L strict book.

---

## 17. Capital accounts and sizing

**Verdict:** OVERHAUL the account model; KEEP the min-of-bounds sizer  
**Files:** `src/trading/runtime/paper_session.py` (equity), `src/trading/risk/limits.py`, `src/trading/risk/reservation.py`, `src/trading/risk/sizing/`  
**Phase:** G1, P2  
**G2:** n/a

**Current.** One ₹7,00,000 equity and the same margin availability. Limits multiply that equity by account fractions, then by strategy allocation fractions. Reservations commit and release from confirmed events. Sizers already floor to whole lots and return the binding zero-lot reason. Strategies still *request* ₹10,000; Layer 2 recalculates.

**Gap.** R-015, T31. No per-mode equity, daily loss, or high-water mark. Shared margin can be spent twice in the narrative of two virtual accounts if P2 is careless. Percent order M1 > M2 > M3 > M4 is not what `risk.yaml` encodes.

**Proposed change.** G1 prices one lot per mode × family and writes the affordability report. P2 adds four ledgers under the same ₹7,00,000, shares 10/20/30/40, reference capital = min(start-of-session allocation, conservative equity), no borrow. A G1 failure reserves nothing and emits `MIN_LOT_EXCEEDS_BUDGET`. Sizer signature stays “minimum of independent lot bounds.” Mode id replaces strategy allocation as the budget key for new entries. Legacy open positions keep their current reservation attribution.

---

## 18. Risk gateway

**Verdict:** EXTEND  
**File:** `src/trading/risk/gateway.py`  
**Phase:** P1 (NIFTY reject), P6 and P10 (leg order)  
**G2:** n/a

**Current.** New intents only. Checks include expiry, snapshot, quality, naked-short ban, structure, quotes, mode/family allowlist (P1), allocation, sizing, paper P0 data, limits, exposure, then reserve. Exits do not pass through this entry gate. Entry freeze and UNKNOWN block new risk in the runner. Debit, credit vertical, and iron condor entry approval are all protection-first (long before short) — P6 for credit, P10 for condor.

**Gap.** R-017. Reduction-vs-entry classification is “exits skip the gateway” rather than a per-action exposure classification for multi-leg closes that might sell protection first.

**Proposed change.** Close plans for short-containing structures drop the short before the long protection. Keep exits alive under entry freeze.

---

## 19. Exposure and arbitration

**Verdict:** EXTEND  
**Files:** `src/trading/portfolio/exposure.py`, `src/trading/portfolio/risk_journal.py`, session correlation check in `paper_session.py`  
**Phase:** P4 exact duplicate; P12 economic overlap  
**G2:** n/a

**Current.** Exposure limits cover net delta, net vega, expiry-day notional, directional agreement, single-event fraction (`config/risk.yaml`). Stress tail breach can freeze entries. The session correlation check treats any open position on the underlying as `CORRELATION_LIMIT`. There is no economic-overlap comparison of a bull call versus a bull put, and no counterfactual book.

**Gap.** R-016, T27–T30, T49, T57. Net delta can look small while gross wings are large. Two mode managers must not both exit one position.

**Proposed change.** P4: exact duplicate suppression plus a single owner on the position record. P12: scenario overlap, conflict policy (default: no unexplained opposing exposure), M4 cap of two positions, campaign drawdown that survives a new position id, and a counterfactual evaluator that cannot call `reservation.py`. Gross and tail checks stay in Layer 2, not in the strategy.

---

## 20. OMS, partial fills, and UNKNOWN orders

**Verdict:** KEEP the engine; EXTEND sequencing  
**Files:** `src/trading/oms/engine.py`, `src/trading/oms/planner.py`  
**Phase:** P6, P10, P11  
**G2:** the recovery half of every family slice

**Current.** One idempotency key per logical order. Timeout becomes `OrderState.UNKNOWN` and freezes replacement until reconciliation. Planner prices buys at ask and sells at bid, plus offset ticks. Legs submit in approved order. There is no atomic multi-leg broker call.

**Gap.** T34–T37. Prefix safety depends on approval order. Incomplete fills already reach `REPAIR_REQUIRED`; the repair must flatten an unprotected short rather than label the intended structure `OPEN`.

**Proposed change.** Keep UNKNOWN handling. Family slices supply the safe sequence and a repair plan. Tests crash at reserve, submit, fill, and register boundaries for each new family. Do not add a second OMS.

---

## 21. Paper fills and charges

**Verdict:** KEEP the fill model; EXTEND charge consistency  
**Files:** `config/evaluation.yaml`, `src/trading/analytics/fills.py`, `src/trading/broker/paper/adapter.py`  
**Phase:** charge reconciliation with P2; per-family fill tags with that family’s slice  
**G2:** fill evidence is part of lifecycle proof

**Current.** Conservative model: buy at ask plus slippage, sell at bid minus slippage, trade-through required, depth at least the quantity, later legs shifted by `legging_delay_ticks`. A limit cannot fill through its limit. Sizing uses ₹50/lot from `risk.yaml`. The fill model carries its own round-trip charge estimate (historically ₹100/lot). Without a fill model, tests can fill immediately at the limit.

**Gap.** R-018, T52. Two charge figures can make sizing and P&L disagree. Quote-only versus depth-informed fills are not labelled as cohorts.

**Proposed change.** Pick one effective-dated charge schedule and feed it to both sizing and the fill model. Tag the capability profile on the fill. Keep the bid/ask model. G1 uses this same charge schedule so affordability matches later orders.

---

## 22. Position lifecycle and recovery

**Verdict:** EXTEND  
**Files:** `src/trading/domain/state/machines.py`, `src/trading/trade/manager.py`, `src/trading/runtime/paper_runner.py` `recover_lifecycle`, `src/trading/storage/trading_store.py`  
**Phase:** P2 attribution; family slices for restart evidence  
**G2:** restart is mandatory before PAPER

**Current.** SQLite event log, position rows, review slots, entry freeze. Restart restores positions, reconciles broker legs, and freezes on gaps. Day loss is not reset by process start. Broker `broker_state.json` is a paper mirror. Protective stops are software-side. `TradeState` has no separate `DEGRADED` or `SETTLEMENT_PENDING`; protection degradation is an operational flag that freezes entries.

**Gap.** R-019. One owner mode is missing. Identical contracts in two modes would net at a future broker and need an internal allocation ledger; paper can still keep separate positions if the ledger says so. Software stops must stay labelled as software stops.

**Proposed change.** Add owner mode and policy version on the position record in P2. Do not expand the state enum until a slice has a transition that the current set cannot represent. Each G2 package includes a restart test. Recovery order stays: config, restore, reconcile, unknowns, then exits, then entries.

---

## 23. Exits and valuation

**Verdict:** OVERHAUL for spreads; KEEP for single-leg leg-price stops  
**Files:** `src/trading/trade/exits.py`, `src/trading/trade/manager.py`, `src/trading/trade/sentinel.py`  
**Phase:** P8  
**G2:** monitoring and exit are part of every family package

**Current.** Unrealized marks buy legs at bid and sell legs at ask. Stop scope is `LEG_PRICE` except iron condor `STRATEGY_PNL`. Long and debit use 40/80 ticks. CAS uses 20/30. Reviews refuse to widen a stop. `StopSentinel` can register between polls for open positions. The 60-second session poll can still miss an intra-interval print; `CURRENT_STATE.md` already records that this is not live-safe.

**Gap.** T42, T43. A long-leg tick stop is not spread P&L. A fixed small target on M1 fights the payoff shape the spec wants. Overnight gaps must fill at the executable price.

**Proposed change.** P8 computes a conservative whole-structure close for debit and credit before comparing with the stop. Single-leg positions may keep leg-price stops. Open legacy trades keep the policy stored at entry. M1 exit parameters move to config in P7/P8 and must leave room for a runner inside the session. Gap fills use the executable quote. Record the poll blind spot as `LIMITATION_CONFIRMED` when a test demonstrates a missed intra-poll print.

---

## 24. Reviews

**Verdict:** EXTEND  
**Files:** `src/trading/trade/review.py`, `src/trading/runtime/review_schedule.py`  
**Phase:** P13  
**G2:** n/a until ROLL/SWITCH submit

**Current.** Slots NSE 10:30 and 14:30. Missed slots can replay once before EOD. Actions: hold, tighten, partial, full. Hedge and roll are proposals and do not submit. Duplicate slot execution is guarded. M1 has a time exit, not these reviews.

**Gap.** R-020, T38, T39, T24, T25. Spec wants one current recovery assessment when both slots were missed, not two stale actions in sequence. `SWITCH` does not exist. `PROPOSE_ROLL` has no linked close/open transaction.

**Proposed change.** P13 changes missed-slot behavior to one recovery review plus an annotation of the missed ids. Implement `ROLL` and `SWITCH` only for families that already have G2 close and open plans. Until then the stored label is `PROPOSED_NOT_EXECUTED`. Hold writes an explicit reason and the next slot. Tighten stays monotonic.

---

## 25. Carry (M2)

**Verdict:** OVERHAUL  
**File:** `src/trading/trade/eod_scanner.py`  
**Phase:** P9  
**G2:** carry approval/rejection is part of M2 lifecycle evidence

**Current.** `evaluate_carry_forward` returns carry only when conviction score ≥ threshold, P&L > 0, and regime alignment all hold. Otherwise it closes. `compute_positional_stop` can widen a stop from ATR and the prior day’s range. The function is pure and injected-clock safe. It is not the paper session’s Mode 2 gate, and a profit requirement disagrees with §4.2 (a loss is not a reason to use a different standard, and a profit is not sufficient).

**Proposed change.** New M2 carry decision `CARRY_APPROVED` or `CARRY_REJECTED` requiring a fresh thesis, remaining expiry, overnight structure-loss budget, event check, portfolio approval, persisted exit state, and next-session recovery health. Missing evidence rejects and starts an exit while the session is tradable. Ownership stays M2. Do not widen stops inside this gate. Leave `evaluate_carry_forward` unused by the new path, or delete it in P9 if nothing else calls it after a reference check.

---

## 26. Paper session orchestration

**Verdict:** OVERHAUL the candidate branch; KEEP the loop, heartbeat, and journal  
**File:** `src/trading/runtime/paper_session.py`  
**Phase:** P4  
**G2:** n/a

**Current.** `build` loads the pipeline, builds a narrow chain, binds long and debit, routes, and emits one request per configured strategy id. Winner strategies execute only when stance is PAPER and the route says so. CAS uses `cas_paper_execute`. Every other id gets an empty candidate tuple and `execute=False`. Equity is hardcoded ₹7,00,000. Poll sleeps `poll_interval_seconds`.

**Gap.** R-005, R-026. The branch cannot host four producers. A 60-second sleep is acceptable for M3/M4 reviews and unacceptable as the only M1 entry trigger (G3).

**Proposed change.** P4 replaces the winner branch with mode producers and the exact-duplicate arbiter. CAS stays on the current branch until G3’s outcome is implemented in P7. Equity construction moves to the P2 ledgers. Do not change the poll for M2/M3/M4 in the M1 slice.

---

## 27. Activity funnel and operator view

**Verdict:** NEW report on existing cycle evidence  
**Files:** `src/trading/runtime/cycle_evidence.py` (uncommitted), `src/trading/dashboard/`, CLI `trading evaluate …`  
**Phase:** P15  
**G2:** reports must show status, not upgrade it

**Current.** Cycle evidence and the dashboard are in the dirty tree and were not treated as redesign surface. Cohort output exists under `data/paper/cohorts`. There is no mode-level funnel with the reason codes in R-023. Counterfactual evaluation has a CLI (`evaluate counterfactual`) that must stay isolated from reservations (T49).

**Proposed change.** P15 emits the funnel `evaluation → eligible signal → bound contracts → valid structure → mode risk → portfolio → order → fill → managed exit`, including flat reasons and G1 abstentions. Two denominators: all scheduled market time, and healthy time. Dashboard and CLI show four allocations, owner, freshness, next review, and G2 status. Do not add this in P0.

---

## 28. Agents

**Verdict:** KEEP the authority boundary  
**Files:** `src/trading/ai/`, `config/agent.yaml`  
**Phase:** no Agent Desk rebuild. A later slice may attach proposal ranking only after deterministic candidates exist (after P4).  
**G2:** n/a

**Current.** Agent Desk Stages A–E are DONE and advisory. `AuthorityMode.BOUNDED` is config-promotion only. No LLM on the order path. `config/agent.yaml` has the agent disabled. Advise code ranks structures and cannot enable orders. Entry and position desks do not submit.

**Gap.** R-021’s named roles (macro analyst, review critic, forward evaluator) overlap desks that already exist. Building a second orchestrator would duplicate them.

**Proposed change.** Keep desks. When M3/M4 reviews need a ranking, pass the deterministic candidate ids and validate the reply. Timeout falls through to the deterministic action. News text stays data. Do not grant order, secret, or shell tools. Token budgets stay in the existing agent budget ledger.

---

## 29. Operations

**Verdict:** KEEP  
**Files:** `src/trading/ops/`, `src/trading/runtime/watchdog.py`, `deploy/`, `docs/context/OPERATIONS_RUNBOOK.md`  
**Phase:** runbook edits only when a slice changes operator steps  
**G2:** n/a

**Current.** Dirty tree adds paper session timer, watchdog, operator alert, and an orchestrator. Heartbeat file path is `data/paper/session_heartbeat.json`. Protection heartbeat is separate. These edits are in progress and are not part of this redesign slice.

**Proposed change.** Leave the ops diff alone. P16 updates the runbook’s strategy list when stances actually change. Watchdog restart still restores before new entries.

---

## 30. Tests

**Verdict:** EXTEND  
**Phase:** the slice that owns the behavior  
**G2:** a unit test file does not set stance

**Current.** Focused modules include `tests/test_identification.py`, `tests/test_l2_slice*.py`, `tests/test_oms.py`, `tests/test_paper_fills.py`, `tests/test_paper_lifecycle.py`, `tests/test_paper_review.py`, `tests/test_paper_protection.py`, `tests/test_safety_invariants.py`. Iron condor and credit sizing have slice tests without a session route.

**Proposed change.** Each family slice adds the G2 scenarios to the existing test style (injected clock, no network). T01–T58 in the traceability file stay `ABSENT` or `PARTIAL` until that test is committed. Do not generate the full matrix in one change. Mark a demonstrated poll-gap miss as `LIMITATION_CONFIRMED`.

---

## 31. Documentation

**Verdict:** EXTEND in place  
**Phase:** this package is P0; behavior docs move with the slice that changes behavior  
**G2:** n/a

**Current.** Canonical context is `docs/context/`. Strategy specs 02, 03, 05, 06 describe today’s families. Spec 04 is commodity. No mode specs. `CURRENT_STATE.md` still records Agent Desk Stage E complete and the 60-second stop blind spot. `ACTIVE_PLAN.md` is the Agent Desk plan.

**Proposed change.** This P0 set is the redesign plan, the component proposal, the context index, the traceability table, and spec §31. Update `CURRENT_STATE.md`, `ARCHITECTURE.md`, and strategy specs only when the corresponding slice changes runtime behavior. Do not mark any family `LIVE_APPROVED`.

---

## 32. Commodity futures

**Verdict:** DISABLE on the NIFTY deployment; KEEP the code  
**Files:** `src/trading/strategies/commodity_futures.py`, `src/trading/risk/sizing/commodity_future.py`, spec `04_COMMODITY_FUTURES.md`  
**Phase:** P1 confirms the gateway reject; stance already SHADOW  
**G2:** stays SHADOW. Not a four-mode family.

**Current.** Session lists it as SHADOW on CRUDEOIL/MCX. Allow-table excludes it from the NIFTY route. `allow_stop_bounded_futures_short` is true for that stop-bounded futures case. Paper margin fraction 0.15 is a PAPER substitute, not exchange SPAN.

**Proposed change.** Leave modules and spec 04 in place with a historical label. P1’s NIFTY execution reject makes an accidental PAPER stance fail closed. Do not delete the sizer in this redesign.
