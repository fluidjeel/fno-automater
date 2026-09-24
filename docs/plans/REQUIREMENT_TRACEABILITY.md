# Four-mode requirement traceability

**Inspected:** 24 September 2026, `main` @ `47dcf15`  
**Rules:** `PRESENT` means the checkout already meets the requirement. `PARTIAL` means related code exists and the requirement is not met. `ABSENT` means no implementation. A unit test without a session path is not `PRESENT`.

Legacy PAPER stances are recorded as current configuration, not as G2 certification of the redesigned policy.

## Requirements

| ID | Status | Where it lives now | Planned change | Phase |
| --- | --- | --- | --- | --- |
| R-001 | PRESENT | Gateway rejects non-NIFTY execution (MCX, BankNifty, NIFTY futures) | Gateway reject for non-NIFTY execution locked in | P1 |
| R-002 | PRESENT | `ModeId` (4 modes) and `FamilyId` (14 families) in enums; `ModePolicy` / `ModesConfig` in contracts | `ModeId` and `ModePolicy` in `config/modes.yaml` | P1 |
| R-003 | PRESENT | Four mode ledgers on ₹7L book (10/20/30/40); per-mode loss caps and reference capital | Locked in via `FourModeBook`, `ModeLedger`, and `build_sizing_limits` | G1, P2 |
| R-004 | PRESENT | Naked short banned; debit/credit verticals and iron condor long-then-short (prefix-safe); locked in tests/test_p6_credit_verticals_and_g2.py and tests/test_p10_iron_condor_binder_and_g2.py | Butterfly prefix safety in P11 | P6, P10, P11 |
| R-005 | PRESENT | Mode producers emit candidates concurrently; arbiter suppresses exact duplicates; verified in P4 (tests/test_p4_producers_and_arbiter.py) | P4, P15 |
| R-006 | PRESENT | Agent Desk cannot order or edit live limits | Keep; no second agent stack | — |
| R-007 | PRESENT | PAPER session is the forward path; backtest is not a promotion gate | Keep | — |
| R-008 | PRESENT | Authoritative calendar port, official 15:30/15:40 clocks unchanged until verified, special session skip rule; verified in P5 (tests/test_p5_calendar_and_m2_expiry.py, test_p5_calendar_port.py) | P5 |
| R-009 | PRESENT | `cas_microstructure` close window; capability map has no aggressor | G3 field inventory and cash CAS disambiguation completed at docs/reports/G3_CAS_FIELD_AND_LATENCY_REPORT.md | G3 |
| R-010 | PRESENT | M2 following-week expiry selector, 0/1 DTE exclusion, master-based holiday/monthly substitution; verified in P5 (tests/test_p5_calendar_and_m2_expiry.py, test_p5_binders.py) | P5 |
| R-011 | PARTIAL | Long, debit, credit verticals (P6), iron condor (P10); CAS and remaining M4 families in upcoming slices | P7, P11 |
| R-012 | PRESENT | `snapshot_bundle.py` keeps distinct leg ids and skew checks; locked with T08/T09 | Verified in `tests/test_p3_payoff_checker.py` | P3 |
| R-013 | PARTIAL | Macro desk and news subsystem exist; not on the order path | Reuse desks; no new tick-path LLM | — |
| R-014 | PRESENT | M1 selector `bind_m1_cas_option` in delta band [0.20, 0.40], 0-DTE default off; distinct from M2's [0.45, 0.65]; `CasMicrostructureStrategy` stamps `ModeId.M1_CAS` and `FamilyId`; startup validation blocks M1 PAPER on >=60s poll; locked in `tests/test_p7_m1_selector_and_g3_path.py` | G3, P7 |
| R-015 | PRESENT | One ₹7,00,000 book partitioned into four independent mode ledgers | G1 report committed; four ledgers, no-borrow, restart-safe in P2 | G1, P2 |
| R-016 | PRESENT | Multi-mode candidate production; PortfolioArbiter suppresses exact duplicates referencing incumbent ID (Scenario T27); economic overlap in P12 | P4, P12 |
| R-017 | PARTIAL | Gateway checks for new intents; exits stay outside it | Mode allowlist; reduction classification for multi-leg closes | P1, P8 |
| R-018 | PRESENT | OMS idempotency, UNKNOWN freeze, bid/ask fills; credit spread and iron condor safe prefix (long-then-short) and partial fill protection locked in tests/test_p6_credit_verticals_and_g2.py and tests/test_p10_iron_condor_binder_and_g2.py | P2, P6, P10 |
| R-019 | PRESENT | PositionState & PositionLifecycleRecord carry mode_id/campaign_id; idempotent store migrations | Owner mode preserved across restart; reservations carry mode_id & idempotency_key | P2 |
| R-020 | PARTIAL | 10:30/14:30 hold, tighten, partial, full; hedge/roll do not submit | One missed-slot recovery; ROLL/SWITCH when G2 allows | P13 |
| R-021 | PARTIAL | Desks A–E advisory; agent disabled in config | Rank deterministic ids later; timeout falls through | after P4 |
| R-022 | PARTIAL | Scorecards, judgment, cohorts | Separate policy-compliance from P&L in P15 | P15 |
| R-023 | ABSENT | No mode funnel with the required reason codes | Funnel including G1 abstentions | P15 |
| R-024 | PRESENT | Strong contracts: `TradeIntent` extended with `mode_id` and `family_id` while preserving Invariant 3 | Extend models; no second domain | P1 |
| R-026 | PARTIAL | 60s poll; protection 2s for open positions | G3 measured: 60s entry poll decoupled from 2s protection; M1 PAPER blocked on polled loop; live under-load latency measurement open | G3 |
| R-027 | PRESENT | `config/modes.yaml` added; startup validation enforces G1, G2, G3, NIFTY-only rules | `modes.yaml` plus existing files; validate PAPER requires G2 | P1 |
| R-028 | PARTIAL | Broad unit and lifecycle tests; not the T01–T58 matrix | Tests land with the owning slice | per phase |
| R-029 | PARTIAL | Context docs match the pre-redesign system | Update each doc in the slice that changes behavior | P16 and per slice |
| R-030 | ABSENT | Checklist items are targets | Close rows only with evidence | P16 |
| R-031 | PRESENT | This index, plan, traceability, and component file | Phase briefs appear when a slice starts | P0 |

## Engineering scenarios

| ID | Status | Note |
| --- | --- | --- |
| T01 | PRESENT | Allowed modes/families validated; gateway-level non-NIFTY and futures reject locked in with tests (tests/test_p1_mode_policy_and_gateway.py) |
| T02 | PARTIAL | Long-option loss is premium-based in the sizer; not re-proven as a four-mode scenario |
| T03 | PRESENT | Four verticals, multiple strikes/lot sizes: formula and generic kink/slope payoff agree; status IMPLEMENTED_UNIT (tests/test_p3_payoff_checker.py) |
| T04 | PRESENT | Iron condor wider-wing formula (`max(put_width, call_width)`) agrees with generic kink/slope payoff for equal and asymmetric wings; locked in tests/test_p10_iron_condor_binder_and_g2.py |
| T05 | PARTIAL | Iron condor prefix is long-before-short (P10); no butterfly yet |
| T06 | ABSENT | No calendar structure |
| T07 | PARTIAL | Strategies fail closed on declared data gaps; capability profiles are not explicit cohorts |
| T08 | PRESENT | Distinct leg snapshot IDs in multi-leg bundles pass without overwriting; locked in tests/test_p3_payoff_checker.py |
| T09 | PRESENT | Stale and skew mismatches between legs reject in validate_leg_snapshot_bundle; locked in tests/test_p3_payoff_checker.py |
| T10 | PRESENT | CAS compares moderately OTM set in delta range [0.20, 0.40]; verified distinct from M2 in `tests/test_p7_m1_selector_and_g3_path.py` |
| T11 | PARTIAL | Spread-width and OI filters exist on the shared binder |
| T12 | PARTIAL | Router cooldown is 30 minutes; no signal-episode id |
| T13 | PRESENT | Mode daily-loss cap enforced via ModeLedger.daily_loss_breached; SafetyControls.evaluate_mode_daily_loss latches per-mode freeze without touching other modes |
| T14 | PRESENT | Distinct feature profiles (cas-microstructure-v1 / EXP-M1-QUOTE vs cas-depth-only-v1 / EXP-M1-DEPTH) and aggressor-independence verified in `tests/test_p7_m1_selector_and_g3_path.py` |
| T15 | PARTIAL | CAS time exit is 900 seconds; session end interaction is not a dedicated test here |
| T16 | PRESENT | M2 current expiry at 0/1 DTE selects following eligible week or abstains with ReasonCode.EXPIRY_0_1_DTE_EXCLUDED; locked in tests/test_p5_calendar_and_m2_expiry.py and tests/test_p5_binders.py |
| T17 | PRESENT | M2 holiday/monthly substitution selects actual listed contract from instrument master, never a guessed symbol; locked in tests/test_p5_calendar_and_m2_expiry.py and tests/test_p5_binders.py |
| T18 | PRESENT | M2 carry gate records CARRY_APPROVED/CARRY_REJECTED from thesis/expiry/budget/event/recovery evidence; rejected carry initiates exit; mode stays M2; locked in tests/test_p9_m2_carry_gate.py |
| T19 | PRESENT | Losing trade may carry when all evidence passes; profit alone does not approve; legacy evaluate_carry_forward profit rule not used; locked in tests/test_p9_m2_carry_gate.py |
| T20 | ABSENT | No M3 allowlist |
| T21 | PRESENT | Bull put credit and bear call credit lifecycle proven under G2 (entry, monitoring, exit, restart) and unblocked from G2_UNPROVEN_FAMILIES; catch-all defined_risk_multileg blocked from PAPER; locked in tests/test_p6_credit_verticals_and_g2.py |
| T22 | PARTIAL | `bind_iron_condor` and `IronCondorStrategy` reach M4 on RANGE markets; not yet session-routed |
| T23 | ABSENT | No straddle or strangle |
| T24 | PARTIAL | Reviews can HOLD; no cost-aware switch hurdle |
| T25 | ABSENT | No SWITCH execution |
| T26 | ABSENT | No two-position M4 cap |
| T27 | PRESENT | Exact duplicate candidate from multi-mode cycle suppressed with ReasonCode.EXACT_DUPLICATE_SUPPRESSED, explicitly recording incumbent ID; locked in tests/test_p4_producers_and_arbiter.py |
| T28 | ABSENT | No economic-overlap check |
| T29 | PARTIAL | Reservations exist; no multi-mode atomic arbiter |
| T30 | PARTIAL | Correlation block is broader than the spec’s conflict policy |
| T31 | PARTIAL | Mode-level one-lot report committed (`docs/reports/G1_ONE_LOT_AFFORDABILITY.md`); live sizing/gateway integration in P2 |
| T32 | PRESENT | Mode entry freeze does not block exits; global daily loss kill switch blocks all new entries; per-mode freeze blocks that mode's entries only |
| T33 | PARTIAL | Margin lots can bind; temporary multi-leg margin is not a named check |
| T34 | PRESENT | Credit spread partial fill never reports spread OPEN while short uncovered, routes to REPAIR_REQUIRED; long-then-short prefix safety locked in tests/test_p6_credit_verticals_and_g2.py |
| T35 | PARTIAL | UNKNOWN plus restart covered for the current OMS |
| T36 | PARTIAL | Idempotent store constraints exist |
| T37 | PARTIAL | Lifecycle recovery tests exist for the current machine |
| T38 | PARTIAL | Duplicate review slot is guarded |
| T39 | PARTIAL | Missed slots can replay once; spec wants one current recovery |
| T40 | PRESENT | Invariant 17 monotonic tightening enforced for both price and P&L stops in assert_stop_not_wider; candidate looser pnl_stop (e.g. -200 vs -150) or widened distance ticks rejected with ValueError; locked in tests/test_p8_whole_structure_spread_valuation.py |
| T41 | PARTIAL | Partial exit exists; structure-ratio preservation is not proven for new families |
| T42 | PRESENT | Multi-leg debit and credit spreads stop and mark on whole-structure P&L (all legs evaluated together at executable bid/ask); spread exits on structure stop even when long leg is inside its tick stop; locked in tests/test_p8_whole_structure_spread_valuation.py |
| T43 | PRESENT | Gap exits use current executable quote (market bid/ask at exit time), not ideal stop price; locked in tests/test_p8_whole_structure_spread_valuation.py |
| T44 | PARTIAL | Degraded protection freezes entries and avoids a fake close |
| T45 | PARTIAL | Agent schema failures abstain |
| T46 | PARTIAL | Macro injection tests exist on the desk |
| T47 | PRESENT | Exits do not wait on an agent |
| T48 | PARTIAL | Exit policy is stored on the position; mode policy versions are not |
| T49 | PARTIAL | Counterfactual CLI exists; isolation must be re-proven when the new book lands |
| T50 | ABSENT | No combined mode-plus-analytics load measurement |
| T51 | ABSENT | No special-session calendar test |
| T52 | PARTIAL | Decimal money is enforced; two charge figures remain |
| T53 | ABSENT | Funnel coverage of every abstention |
| T54 | ABSENT | No legacy-position mode migration |
| T55 | PARTIAL | Paper isolate check exists in the CLI |
| T56 | ABSENT | No campaign drawdown across rolls |
| T57 | PARTIAL | Net exposure limits exist; gross-tail illusion is not the named test |
| T58 | PARTIAL | Credit and condor are implemented and not session-routed, which is the condition this test must detect |

## External verification still open

| Item | Status |
| --- | --- |
| NIFTY lot size and tick from the current contract file | Required for G1 |
| Quoted premiums for the affordability report | Required for G1 |
| NSE cash CAS versus NIFTY option depth fields | Verified in G3 report (docs/reports/G3_CAS_FIELD_AND_LATENCY_REPORT.md) |
| TBT depth entitlement on the paper host | Verified in G3 report: aggressor strictly UNAVAILABLE; depth segregated |
| Charge schedule that sizing and fills will share | P2 |
