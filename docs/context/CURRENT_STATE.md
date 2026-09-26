# Current State

LAST_UPDATED: 2026-09-26
CURRENT_MILESTONE: PAPER Discovery Mode (`docs/context/DISCOVERY_MODE.md`)
STATUS: DISC-A0..A11_DONE; review fixes landed (EOD cohort IDs, per-mode feed
errors, full-family decision matrix); Oracle rsync + restart still pending
(DISC-A12)

## Evidence labels

- TEST-PROVEN: 1933+ pytest including discovery suite (`tests/test_disc_*.py`,
  `tests/test_cohort_eod_duplicate_signals.py`).
- DEPLOYED-PAPER: Oracle still on pre-review tree until rsync; local config has
  `entry_profile: DISCOVERY`, `experiment_prefix: EXP-DISC`.
- OBSERVED-IN-MARKET: not yet.

## Discovery profile (local config)

| Item | Value |
| --- | --- |
| `entry_profile` | `DISCOVERY` |
| `experiment_prefix` | `EXP-DISC` |
| Books | Four independent ₹7L per mode; daily compounding from prior realised net |
| Fills | `touch-v1` with `conservative-v1` shadow verdict |
| Straddle/strangle | `PAPER` under DISCOVERY |
| Calendars | `SUSPENDED` (unchanged) |

## Mode stances (file and loaded)

| Mode | Config | Loaded after startup validation |
| --- | --- | --- |
| M1_CAS | PAPER | PAPER |
| M2_DIRECTIONAL | PAPER | PAPER |
| M3/M4 | PAPER | PAPER |

Routing profile is `four_mode`. **M1 remains event-only until DISC-B1** — the
60-second poll clears pending M1 events and does not submit them; only
`submit_m1_event` runs M1. M2–M4 evaluate every poll under DISCOVERY.

## Prior milestones

Four-mode redesign P1–P16 complete (P14 calendars
`EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN`). LIVE not approved.

## Blocking gaps before Monday

1. Oracle deploy: rsync `9c9619d` + review fixes, restart `fno-paper-session`.
2. Fyers token refresh before 09:00 IST Sunday/Monday.
3. Post-deploy: startup banner `ENTRY PROFILE: DISCOVERY (temporary)`; heartbeat
   `entry_profile: DISCOVERY`; decision records per mode/family each cycle.
4. Sprint B (M1 poll, EOD discovery report, dashboard) not started.

## Next action

Deploy to Oracle with `./deploy/deploy_oracle.sh --host ubuntu@92.4.94.79
--key ../blue-green/keys/ssh-key-2026-09-12.key --with-secrets`, restart the
paper session, refresh Fyers token, and verify one full market cycle records
decisions for every routable family.
