# Four-mode redesign — execution plan

**Status:** G1, G3, and P1–P16 completed on the working branch as of 24 September 2026. Calendars remain `EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN` and off the strict book. LIVE blocked.  
**Target spec:** [NIFTY_FOUR_MODE_CURSOR_REDESIGN.md](NIFTY_FOUR_MODE_CURSOR_REDESIGN.md) v1.1  
**Component proposals:** [FOUR_MODE_COMPONENT_CHANGES.md](FOUR_MODE_COMPONENT_CHANGES.md)  
**Resume sheet:** [../context/FOUR_MODE_CONTEXT_INDEX.md](../context/FOUR_MODE_CONTEXT_INDEX.md)  
**Requirement status:** [REQUIREMENT_TRACEABILITY.md](REQUIREMENT_TRACEABILITY.md)

Agent Desk Stage E stays historical DONE in [TASK_LEDGER.md](TASK_LEDGER.md) and [ACTIVE_PLAN.md](ACTIVE_PLAN.md). Do not reopen it. Queue one READY redesign slice at a time. `PLAN_STATUS` on the Agent Desk plan is unchanged until a human points the active milestone at G1.

## Goal

A NIFTY-only paper runtime with four mode policies, one portfolio arbiter, and independent capital accounts. Families reach unattended PAPER only after lifecycle evidence. Live trading stays blocked.

## Non-goals

- Real-money orders, live enablement, or `LIVE_APPROVED`.
- Rebuilding Agent Desk or putting an LLM on the order path.
- A second domain model beside `TradeIntent` / Layer 2.
- Admitting calendars to the strict book before a dual-expiry loss bound exists.
- Changing 15:30 / 15:40 clocks before an effective circular and a broker check.
- Resetting the dirty working tree (paper autopilot, watchdog, cycle evidence).

## How later sessions stay cheap

Read, in order: this file’s current phase, the context index, the phase brief under `docs/plans/phase_briefs/` once it exists, and the source files that brief names. Open the full spec only when a cited requirement is ambiguous. One slice per session. Reuse Layer 2, OMS, the fill model, and the review engine. Do not add LangChain or Temporal.

## Binding decisions

- Paper equity stays ₹7,00,000. Shares 10/20/30/40 are a risk-order seed until G1.
- Reference capital = min(start-of-session allocation, conservative equity). No cross-mode borrow.
- New M2 entries use the long-option family. `directional_conviction` is not a second plugin.
- Open positions keep their stored owner and exit policy.
- Mode 3 is vertical spreads. Mode 4 is the broader basket. Shared arbiter.
- Commodity code stays, stance SHADOW, off the NIFTY execution route.
- Config extends existing YAML plus one `config/modes.yaml`. No duplicate limit files.
- Legacy PAPER stances are not G2 certificates.

## Gates

### G1 — one-lot feasibility (first code slice)

Price one complete structure per mode × allowed family using the current instrument-master lot and a timestamped quote chain, plus charges from the single charge schedule. Record `AFFORDABLE` or `MIN_LOT_EXCEEDS_BUDGET`. Failures cannot be set to PAPER. Do not raise caps or split lots. A closed market uses the latest stored chain and says so.

**Exit:** a committed report (path recorded in the traceability row for R-015) and a test that a fixture lot above the cap abstains.

### G3 — CAS data and timing (completed)

Inventory fields the paper entry path actually consumes for NIFTY option quotes, option depth, and any futures or index proxy. Label observed, inferred, or unavailable. Measure entry latency against the 60-second session poll. Protection’s 2-second REST poll is not an entry path. NSE cash Closing Auction Session is a different mechanism.

**Exit:** completed and verified at `docs/reports/G3_CAS_FIELD_AND_LATENCY_REPORT.md` and `tests/test_g3_cas_measurement.py`. Mode 1 PAPER stance on 60s polled loop is BLOCKED; M1 is restricted to research profiles (`EXP-M1-QUOTE` and `EXP-M1-DEPTH`). No selector code in this slice.

### G2 — promotion ladder (every family slice)

`IMPLEMENTED_UNIT` → `LIFECYCLE_PROVEN` (entry, partial-fill recovery, monitoring, exit, restart) → `PAPER_STANCE_ENABLED`. Calendars stay `EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN`. A payoff test does not flip stance.

## Phases

| ID | Work | Blocks | Exit |
| --- | --- | --- | --- |
| P0 | This review package | — | Docs agree; tree’s trading code unchanged |
| G1 | One-lot report | Operable capital, any new PAPER stance | Affordability rows persisted |
| G3 | CAS field and latency measurement | P7 and any CAS stance change | Completed & verified; report + tests |
| P1 | `ModeId` / `ModePolicy`, startup validation, NIFTY execution reject | — | Completed & verified; `config/modes.yaml` added; non-NIFTY rejects |
| P2 | Four ledgers on the ₹7L book, restart-safe, no borrow | G1 failures stay non-executable | Reservation survives restart |
| P3 | Same-expiry payoff check for the four verticals | — | Formula and kink/slope check agree. Status `IMPLEMENTED_UNIT` |
| P4 | M2 long and M3 debit producers, exact-duplicate arbiter | G2 if stance changes | Completed & verified; one executable portfolio; exact duplicate suppression referencing incumbent ID (Scenario T27); counterfactual log does not reserve |
| P5 | M2 following-week expiry, no 0/1 DTE, master-based substitution | — | Completed & verified; calendar port, 0/1 DTE ban, master holiday/monthly substitution, abstain reasons (ReasonCode.EXPIRY_0_1_DTE_EXCLUDED, CALENDAR_NO_ELIGIBLE_EXPIRY, INSTRUMENT_MASTER_ABSENT) |
| P6 | M3 credit verticals, long-then-short, G2 set | G1 for that family | Completed & verified; long protection first in RiskGateway & strategies; BullPutCreditStrategy & BearCallCreditStrategy; bind_credit_spread; G2 lifecycle proven; partial fill routes to REPAIR_REQUIRED; defined_risk_multileg blocked |
| P7 | M1 selector on the G3 path | G3 | Completed & verified; bind_m1_cas_option with delta [0.20, 0.40], 0-DTE default off; distinct M1/M2 strikes; CasMicrostructureStrategy stamps ModeId.M1_CAS and FamilyId; G3 polled-loop block enforced; locked in tests/test_p7_m1_selector_and_g3_path.py |
| P8 | Whole-structure spread valuation | — | Completed & verified; multi-leg spreads exit and mark on whole-structure P&L; legacy open trades preserve stored policy; Invariant 17 monotonic tightening; gap exits use executable quotes; locked in tests/test_p8_whole_structure_spread_valuation.py |
| P9 | M2 carry gate | — | Completed & verified; `CARRY_APPROVED`/`CARRY_REJECTED`; rejected carry initiates exit; mode stays M2; locked in tests/test_p9_m2_carry_gate.py |
| P10 | Iron condor binder, prefix safety, G2 | G1 | Completed & verified; `bind_iron_condor`; wider-wing payoff; long-before-short gateway order; G2 lifecycle; locked in tests/test_p10_iron_condor_binder_and_g2.py |
| P11 | Butterfly, straddle, strangle, iron butterfly, each with G2 | G1 per family | Range path does not need UP/DOWN |
| P12 | Economic overlap, conflict policy, M4 cap 2, campaign drawdown | — | Counterfactual cannot mutate reservations |
| P13 | One missed-review recovery; ROLL/SWITCH only where G2 close/open exists | — | Otherwise `PROPOSED_NOT_EXECUTED` |
| P14 | Calendar research registry | — | Off the strict book |
| P15 | Activity funnel and four-mode operator view | — | Shows G1 and G2 per family |
| P16 | Doc reconciliation and remaining scenario tests | — | Honest limitation list; LIVE still blocked |

HEDGE waits until P12 can fund it from the owning mode with a finite payoff.

## Acceptance already met by P0

- Spec §31, this plan, the component file, the context index, and the traceability table describe the same gates and the same KEEP / OVERHAUL split.
- No family is marked paper-integrated for the redesigned policy.
- LIVE is blocked. Trading source was not modified.

## Next action

Redesign implementation complete through P16. Operate in attended PAPER; promote families only with G2 evidence.
