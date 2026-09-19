# PAPER positional lifecycle simulation

Stacked on PR #3 (lifecycle recovery) and PR #4 (twice-daily review).
Production session, strategy, Layer 2, TradeManager, OMS and paper broker
are used as-is. Only the clock, quotes and broker acknowledgements are fixtures.

## 1. Case table

| Case | Label | Realized result | Evidence |
| --- | --- | --- | --- |
| 1. positional long HOLD/HOLD/target | **PASS** | entry opened; 10:30+14:30 HOLD; target fill=96.15 (stop=90.00 target=96.00); isolation=PAPER isolation refused a Fyers transaction adapter | `tests/artifacts/paper_positional_sim/case-01` |
| 2. debit spread LEG_PRICE exit | **FAIL** | spread did not open; risk_reasons=[('SNAPSHOT_MISMATCH',)] | `tests/artifacts/paper_positional_sim/case-02` |
| 3. winning tighten, no loosen | **UNKNOWN** | production long_option template has no break-even/trail (stop stayed 90.00→90.00→90.00; reviews=[<ReviewAction.HOLD: 'HOLD'>, <ReviewAction.HOLD: 'HOLD'>]). Later review did not loosen. | `tests/artifacts/paper_positional_sim/case-03` |
| 4. PARTIAL_EXIT + partial fill + restart | **UNKNOWN** | review never chose PARTIAL_EXIT (actions=[<ReviewAction.HOLD: 'HOLD'>]). Frozen template has no trail/BE so _partial_exit_quantity returns None. qty=75 stop=90.00 | `tests/artifacts/paper_positional_sim/case-04` |
| 5. UNKNOWN exit timeout + restart | **PASS** | FULL_EXIT timeout; unknown_store=1; restart_pid={'pid': 3543, 'restored_trade_ids': ['TRD-1789357800000000-00000011'], 'entries_blocked': False}; sells=0; states=['EXIT_PENDING'] | `tests/artifacts/paper_positional_sim/case-05` |
| 6. 15:40 halt, next-session restore | **PASS** | trade=TRD-1789357800000000-00000011 policy=EXIT-POL-1789357800000000-00000020 stop=90.00 restored=True | `tests/artifacts/paper_positional_sim/case-06` |
| 7. overnight gap through stop | **PASS** | trade=TRD-1789357800000000-00000011 frozen_stop=90.00 exit_limit=85.00 fill=84.95 | `tests/artifacts/paper_positional_sim/case-07` |
| 8. stop print between 60s polls | **PASS** | intra-interval stop print 85.00 was not fed; next poll recovered to 91.20. detected=False still_open=True exposure=one poll_interval_seconds (60s) with software-only coverage | `tests/artifacts/paper_positional_sim/case-08` |
| 9. missed 10:30 catch-up | **PASS** | slot_runs=[(<ReviewSlotId.NSE_MORNING: 'NSE_MORNING'>, datetime.date(2026, 9, 14))] actions=[<ReviewAction.HOLD: 'HOLD'>] first_runs=((<ReviewSlotId.NSE_MORNING: 'NSE_MORNING'>, datetime.date(2026, 9, 14)),) | `tests/artifacts/paper_positional_sim/case-09` |
| 10. stale quotes while open | **PASS** | kept_open=True stale_did_not_stop=True review=[<ReviewAction.HOLD: 'HOLD'>]/[<ReasonCode.PRICE_UNAVAILABLE: 'PRICE_UNAVAILABLE'>] entry_orders=1 | `tests/artifacts/paper_positional_sim/case-10` |
| 11. multi-leg partial / missing monitor | **FAIL** | multi-leg entry blocked by SNAPSHOT_MISMATCH (same as case 2); partial_state=None false_protected=False; missing_monitor kept OPEN, sells=0 | `tests/artifacts/paper_positional_sim/case-11` |
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

- **Case 2** `RiskGateway.evaluate -> _leg_snapshots_complete (src/trading/risk/gateway.py) via PaperRunner._leg_snapshots`: spread did not open; risk_reasons=[('SNAPSHOT_MISMATCH',)]
  Proposed correction: Stamp each PaperRunner._leg_snapshots value with intent.snapshot_id, matching _feature_for. Layer 2 currently rejects debit spreads when option candidates carry distinct snapshot ids (SNAPSHOT_MISMATCH).
- **Case 11** `RiskGateway.evaluate -> _leg_snapshots_complete (same SNAPSHOT_MISMATCH as case 2)`: multi-leg entry blocked by SNAPSHOT_MISMATCH (same as case 2); partial_state=None false_protected=False; missing_monitor kept OPEN, sells=0
  Proposed correction: Fix case 2 snapshot-id stamping first. Missing-monitor already fail-closes the exit path without flattening.

### Paper-model limitations

- PAPER protective STOPs remain local software stubs, not broker-resident working orders.
- Exit evaluation runs on `poll_interval_seconds` (60). Intra-interval prints are invisible.
- Conservative fills require a published PaperBroker quote. This harness publishes each observation as the live book; production `_publish_quotes` only runs while evaluating a new intent.
- Case 5: `SafetyControls.freeze_entries` on UNKNOWN is in-memory. Restart recovered EXIT_PENDING without blocking entries (`entries_blocked=False`); OMS idempotency still refused a second SELL.
- Charges are itemized from `config/evaluation.yaml` round-trip `charges_per_lot`, not a live contract note.

### Review-policy gaps

- `positional_long_option` / `debit_spread` freeze `break_even_trigger_ticks=None` and no trail, so `TIGHTEN_STOP` and `PARTIAL_EXIT` are unreachable on the production template.
- ReviewEngine does not consume IV, theta, quoted spread or margin; those inputs HOLD unless a frozen stop/target/expiry rule fires.
- `PROPOSE_HEDGE` / `PROPOSE_ROLL` persist `REVIEW_PROPOSAL_REQUIRES_L2` and never auto-submit.

## First FAIL (no production patch in this run)

- Call site: `RiskGateway.evaluate -> _leg_snapshots_complete (src/trading/risk/gateway.py) via PaperRunner._leg_snapshots`
- Result: spread did not open; risk_reasons=[('SNAPSHOT_MISMATCH',)]
- Smallest proposed correction: Stamp each PaperRunner._leg_snapshots value with intent.snapshot_id, matching _feature_for. Layer 2 currently rejects debit spreads when option candidates carry distinct snapshot ids (SNAPSHOT_MISMATCH).

## 4. Commands and focused tests

- `uv run python -m tests.paper_positional_sim`
- `uv run pytest tests/test_paper_positional_e2e_sim.py tests/test_paper_lifecycle.py tests/test_paper_review.py tests/test_paper_session.py -q`

See pytest output in the PR checks.

## 5. Verdict

Not suitable for unattended forward observation until the first FAIL is fixed: case 2 at `RiskGateway.evaluate -> _leg_snapshots_complete (src/trading/risk/gateway.py) via PaperRunner._leg_snapshots`. LIVE promotion remains blocked by that defect plus software-only stops, the 60s poll gap, unreachable trail/partial review actions, and PAPER-005 evidence/LIVE-config requirements.

