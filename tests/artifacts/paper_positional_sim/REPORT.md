# PAPER positional lifecycle simulation

Stacked on PR #3 (lifecycle recovery) and PR #4 (twice-daily review).
Production session, strategy, Layer 2, TradeManager, OMS and paper broker
are used as-is. Only the clock, quotes and broker acknowledgements are fixtures.

## 1. Case table

| Case | Label | Realized result | Evidence |
| --- | --- | --- | --- |
| 1. positional long HOLD/HOLD/target | **PASS** | entry opened; 10:30+14:30 HOLD; target fill=96.15 (stop=90.00 target=96.00); isolation=PAPER isolation refused a Fyers transaction adapter | `tests/artifacts/paper_positional_sim/case-01` |
| 2. debit spread LEG_PRICE exit | **PASS** | scope=LEG_PRICE frozen_stop=90.00 monitor=leg-long (LEG_PRICE on the strategy long, not structure PnL); closed=True | `tests/artifacts/paper_positional_sim/case-02` |
| 3. winning tighten, no loosen | **PASS** | disabled production template: no BE/trail so TIGHTEN_STOP is unreachable (stop stayed 90.00→90.00→90.00; reviews=[<ReviewAction.HOLD: 'HOLD'>, <ReviewAction.HOLD: 'HOLD'>]). Later review did not loosen. | `tests/artifacts/paper_positional_sim/case-03` |
| 4. PARTIAL_EXIT + partial fill + restart | **PASS** | remaining=schema_version='1' trade_id='TRD-1789357800000000-00000011' intent_id='c27c4dd00ff132f4a595d234ab7ff4440e1b1fccdd1752a436ae551a4635f1e5' strategy_id='positional_long_option' strategy_version='long-option-v1' experiment_id='EXP-PAPER-SIM-1' execution_mode=<ExecutionMode.PAPER: 'PAPER'> state=<TradeState.OPEN: 'OPEN'> legs=(PositionLegState(leg_id='leg-1', contract=ContractRef(exchange=<Exchange.NFO: 'NFO'>, symbol='NIFTY26SEP24000CE', instrument_kind=<InstrumentKind.OPTION: 'OPTION'>, asset_class=<AssetClass.EQUITY_INDEX: 'EQUITY_INDEX'>, underlying='NIFTY', broker_token=None, expiry=datetime.date(2026, 9, 24), strike=Decimal('24000'), option_type=<OptionType.CALL: 'CALL'>), side=<Side.BUY: 'BUY'>, quantity_contracts=38, average_entry_price=Price(value=Decimal('92.05'), tick=TickSize(value=Decimal('0.05'))), current_stop_price=Price(value=Decimal('90.00'), tick=TickSize(value=Decimal('0.05')))),) exit_policy=ExitPolicy(schema_version='1', policy_id='EXIT-POL-1789357800000000-00000020', trade_id='TRD-1789357800000000-00000011', scope=<ExitScope.LEG_PRICE: 'LEG_PRICE'>, initial_stop_distance_ticks=40, current_stop_distance_ticks=1, stop_price=Price(value=Decimal('93.50'), tick=TickSize(value=Decimal('0.05'))), target_price=Price(value=Decimal('96.00'), tick=TickSize(value=Decimal('0.05'))), strategy_entry_pnl=None, pnl_stop=None, pnl_target=None, trailing_active=True, breakeven_active=True, time_exit=None, exit_before_expiry_days=1, initialized_at=datetime.datetime(2026, 9, 14, 3, 50, tzinfo=datetime.timezone.utc)) protective_order_ids=('PROT-1789357800000000-00000013',) opened_at=datetime.datetime(2026, 9, 14, 3, 50, tzinfo=datetime.timezone.utc) as_of=datetime.datetime(2026, 9, 14, 5, 0, tzinfo=datetime.timezone.utc) protection_degraded=False software_stop_unavailable=False protection_degraded_since=None unprotected_reason=None reservations=[('RES-1789357800000000-00000009', <ReservationState.COMMITTED: 'COMMITTED'>)] | `tests/artifacts/paper_positional_sim/case-04` |
| 5. UNKNOWN exit timeout + restart | **PASS** | FULL_EXIT timeout; unknown_store=1; restart_pid={'pid': 2886, 'restored_trade_ids': ['TRD-1789357800000000-00000011'], 'entries_blocked': True}; sells=0; states=['EXIT_PENDING'] | `tests/artifacts/paper_positional_sim/case-05` |
| 6. 15:40 halt, next-session restore | **PASS** | trade=TRD-1789357800000000-00000011 policy=EXIT-POL-1789357800000000-00000020 stop=90.00 restored=True | `tests/artifacts/paper_positional_sim/case-06` |
| 7. overnight gap through stop | **PASS** | trade=TRD-1789357800000000-00000011 frozen_stop=90.00 exit_limit=85.00 fill=84.95 | `tests/artifacts/paper_positional_sim/case-07` |
| 8. stop print between 60s polls | **PASS** | intra-interval stop print 85.00 was not fed; next poll recovered to 91.20. detected=False still_open=True exposure=one poll_interval_seconds (60s) with software-only coverage | `tests/artifacts/paper_positional_sim/case-08` |
| 9. missed 10:30 catch-up | **PASS** | slot_runs=[(<ReviewSlotId.NSE_MORNING: 'NSE_MORNING'>, datetime.date(2026, 9, 14))] actions=[<ReviewAction.HOLD: 'HOLD'>] first_runs=((<ReviewSlotId.NSE_MORNING: 'NSE_MORNING'>, datetime.date(2026, 9, 14)),) | `tests/artifacts/paper_positional_sim/case-09` |
| 10. stale quotes while open | **PASS** | kept_open=True stale_did_not_stop=True protection_degraded=True freeze=True review=[<ReviewAction.HOLD: 'HOLD'>]/[<ReasonCode.PROTECTION_DEGRADED: 'PROTECTION_DEGRADED'>] entry_orders=1 | `tests/artifacts/paper_positional_sim/case-10` |
| 11. multi-leg partial / missing monitor | **PASS** | partial_state=REPAIR_REQUIRED false_protected=False; missing_monitor open=OPEN unprotected=True silent_hold=False sells=0 freeze=schema_version='1' entries_blocked=True reason_code=<ReasonCode.UNPROTECTED_POSITION: 'UNPROTECTED_POSITION'> detail='monitor leg quote missing; software stop cannot evaluate and PAPER has no broker-resident stop' updated_at=datetime.datetime(2026, 9, 14, 5, 0, tzinfo=datetime.timezone.utc) | `tests/artifacts/paper_positional_sim/case-11` |
| 12. IV/theta/spread/expiry/margin review | **PASS** | 10:30 actions=[<ReviewAction.HOLD: 'HOLD'>] (IV/theta/spread/margin not in ReviewEngine inputs); 14:30 actions=[<ReviewAction.HOLD: 'HOLD'>, <ReviewAction.FULL_EXIT: 'FULL_EXIT'>]; midpoint-probe=PROPOSE_HEDGE/REVIEW_PROPOSAL_REQUIRES_L2 submit=False | `tests/artifacts/paper_positional_sim/case-12` |

## 2. Full chronological traces (overnight gap and UNKNOWN exit restart)

### Case 5: UNKNOWN exit timeout + restart

```
2026-09-14T09:20:00+05:30 → entry; NIFTY26SEP24000CE 91.95/92.00 last=92.00 dte=10 VALID → TRD-1789357800000000-00000011 OPEN legs=[BUY 75 NIFTY26SEP24000CE@92.05] protect=('PROT-1789357800000000-00000013',) policy=EXIT-POL-1789357800000000-00000020 stop=90.00 target=96.00 scope=LEG_PRICE time_exit=None → no-review-row; continuous=OPEN → RESIZE reservation=RES-1789357800000000-00000009 codes=('OK',) → FILLED BUY 75 NIFTY26SEP24000CE limit=92.00 idemp=868b1a95 → BUY 75 @ 92.05 (FILLED) → gross=-6903.75 charges=INR 0 confirmed=True net=-6903.75 fills=[BUY 75@92.05 limit=92.00 slip=0.05]
2026-09-14T10:30:00+05:30 → 10:30 FULL_EXIT via expiry flatten, broker timeout; NIFTY26SEP24000CE 91.95/92.00 last=92.00 dte=1 VALID → TRD-1789357800000000-00000011 EXIT_PENDING legs=[BUY 75 NIFTY26SEP24000CE@92.05] protect=('PROT-1789357800000000-00000013',) policy=EXIT-POL-1789357800000000-00000020 stop=90.00 target=96.00 scope=LEG_PRICE time_exit=None → NSE_MORNING/FULL_EXIT/CONTRACT_EXPIRED submitted=False (days to expiry 1 is at or inside frozen exit_before_expiry_days 1); continuous=EXIT_PENDING → RESIZE reservation=RES-1789357800000000-00000009 codes=('OK',) → none → none → gross=-6903.75 charges=INR 0 confirmed=True net=-6903.75 fills=[BUY 75@92.05 limit=92.00 slip=0.05]
2026-09-14T11:00:00+05:30 → restart catch-up must not resubmit; NIFTY26SEP24000CE 91.95/92.00 last=92.00 dte=1 VALID → TRD-1789357800000000-00000011 EXIT_PENDING legs=[BUY 75 NIFTY26SEP24000CE@92.05] protect=('PROT-1789357800000000-00000013',) policy=EXIT-POL-1789357800000000-00000020 stop=90.00 target=96.00 scope=LEG_PRICE time_exit=None → no-review-row; continuous=EXIT_PENDING → RESIZE reservation=RES-1789357800000000-00000009 codes=('OK',) → none → none → gross=-6903.75 charges=INR 0 confirmed=True net=-6903.75 fills=[BUY 75@92.05 limit=92.00 slip=0.05]
```

### Case 7: overnight gap through stop

```
2026-09-14T09:20:00+05:30 → entry; NIFTY26SEP24000CE 91.95/92.00 last=92.00 dte=10 VALID → TRD-1789357800000000-00000011 OPEN legs=[BUY 75 NIFTY26SEP24000CE@92.05] protect=('PROT-1789357800000000-00000013',) policy=EXIT-POL-1789357800000000-00000020 stop=90.00 target=96.00 scope=LEG_PRICE time_exit=None → no-review-row; continuous=OPEN → RESIZE reservation=RES-1789357800000000-00000009 codes=('OK',) → FILLED BUY 75 NIFTY26SEP24000CE limit=92.00 idemp=868b1a95 → BUY 75 @ 92.05 (FILLED) → gross=-6903.75 charges=INR 0 confirmed=True net=-6903.75 fills=[BUY 75@92.05 limit=92.00 slip=0.05]
2026-09-14T15:40:00+05:30 → EOD; NIFTY26SEP24000CE 91.95/92.00 last=92.00 dte=10 VALID → TRD-1789357800000000-00000011 OPEN legs=[BUY 75 NIFTY26SEP24000CE@92.05] protect=('PROT-1789357800000000-00000013',) policy=EXIT-POL-1789357800000000-00000020 stop=90.00 target=96.00 scope=LEG_PRICE time_exit=None → no-review-row; continuous=OPEN → RESIZE reservation=RES-1789357800000000-00000009 codes=('OK',) → none → none → gross=-6903.75 charges=INR 0 confirmed=True net=-6903.75 fills=[BUY 75@92.05 limit=92.00 slip=0.05]
2026-09-15T09:20:00+05:30 → gap through stop; NIFTY26SEP24000CE 85.00/85.20 last=85.00 dte=10 VALID → TRD-1789357800000000-00000011 CLOSED legs=[BUY 75 NIFTY26SEP24000CE@92.05] protect=('PROT-1789357800000000-00000013',) policy=EXIT-POL-1789357800000000-00000020 stop=90.00 target=96.00 scope=LEG_PRICE time_exit=None → no-review-row; continuous=CLOSED via CLOSED → RESIZE reservation=RES-1789357800000000-00000009 codes=('OK',) → FILLED SELL 75 NIFTY26SEP24000CE limit=85.00 idemp=75aab91a → SELL 75 @ 84.95 (FILLED) → gross=-532.50 charges=INR 100 confirmed=True net=-632.50 fills=[BUY 75@92.05 limit=92.00 slip=0.05; SELL 75@84.95 limit=85.00 slip=0.05]
```

## 3. Findings

### Safety defects

None demonstrated as invariant violations in this run.

### Paper-model limitations

- PAPER protective STOPs remain local software stubs, not broker-resident working orders.
- Case 8 PASS is a limitation detected: `poll_interval_seconds=60` cannot see a stop that prints and reverses between polls. SAFETY: NOT ACCEPTABLE for live unattended stops.
- Conservative fills require a published PaperBroker quote. This harness publishes each observation as the live book; production `_publish_quotes` only runs while evaluating a new intent.
- Charges are itemized from `config/evaluation.yaml` round-trip `charges_per_lot`, not a live contract note.

### Review-policy gaps

- Production `positional_long_option` / `debit_spread` freeze `break_even_trigger_ticks=None` and no trail (case 3 PASS: disabled template). Case 4 overlays BE/trail in the sim harness only.
- ReviewEngine does not consume IV, theta, quoted spread or margin; those inputs HOLD unless a frozen stop/target/expiry rule fires.
- `PROPOSE_HEDGE` / `PROPOSE_ROLL` persist `REVIEW_PROPOSAL_REQUIRES_L2` and never auto-submit.

## 4. Commands and focused tests

- `uv run python -m tests.paper_positional_sim`
- `uv run pytest tests/test_paper_positional_e2e_sim.py tests/test_paper_lifecycle.py tests/test_paper_review.py tests/test_paper_session.py -q`

See pytest output in the PR checks.

## 5. Verdict

PAPER positional lifecycle is suitable for attended forward observation of entry, frozen-policy stops/targets, twice-daily HOLD reviews, 15:40 persist/restore and missed-slot catch-up. It is not unattended-PAPER or LIVE-ready: software-only protection, case 8 60s poll gap (SAFETY: NOT ACCEPTABLE for live unattended stops), and hedge/roll still require a new Layer 2 trade. PAPER-005 stays blocked.

