# Current State

LAST_UPDATED: 2026-09-28
CURRENT_MILESTONE: PAPER Discovery Mode (`docs/context/DISCOVERY_MODE.md`)
STATUS: DISC-A0..A11_DONE; DISC-A13_DONE; DISC-A15_DONE (quote freshness from
fetch/calculation time, not bar event_time); DISC-A14_DONE (margin soft-resize,
safe projection, evaluation-error defense); DISC-A22_DONE (single Fyers data_ws
per account via `fno-data-tick` shared hub); Oracle rsync + restart still pending
(DISC-A12)

## Evidence labels

- TEST-PROVEN: 1940+ pytest including discovery suite (`tests/test_disc_*.py`,
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
| Calendars | `SUSPENDED` (dual-expiry lifecycle unproven; no change in A13) |
| Capital caps in DISCOVERY | Soft only — downsize and/or `strict_would_block`; never reject |
| Margin in DISCOVERY | Soft only — min 1 lot; `MARGIN_INSUFFICIENT` / `MARGIN_OVERSUBSCRIBED` shadowed; projection clamped |

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

## Fyers data_ws audit (DISC-A22)

Policy: exactly one `fyers_apiv3.FyersWebsocket.data_ws.FyersDataSocket` per Fyers
account. TBT (`FyersTbtSocket`) is a separate socket type and out of scope except
where CAS opens `data_ws`.

| Location | Opens data_ws? | Process / systemd unit | Notes |
| --- | --- | --- | --- |
| `src/trading/data/fyers/ws.py` (`FyersTickStream`) | yes (guarded) | `trading data stream --daemon` → `fno-data-tick.service` | Tick daemon; now owns the shared hub |
| `src/trading/runtime/fyers_ws_monitor.py` | yes (via `FyersTickStream`) | `fno-paper-session.service` when `protection.quote_source=direct_ws` | Legacy path; disallowed while tick daemon lock held |
| `src/trading/data/cas_depth/collector.py` | yes when hub inactive | ad-hoc / measurement jobs | Uses hub `DepthUpdate` when `fno-data-tick` lock active |
| `src/trading/data/fyers/capability_probe.py` | yes when hub inactive | manual probe scripts | Defers with health error when tick daemon lock held |
| DISC-A18 `promoted_depth_ws.py` (PR #30, not merged) | yes (planned) | would run in paper session | Rebase onto A22; use `SharedHubClient` with `data_type=DepthUpdate` |

Oracle running services (paper session): `fno-automated`, `fno-data-tick`,
`fno-paper-session`.

| Scenario | data_ws sockets |
| --- | ---: |
| Today on main (tick + paper `ws_enabled` + CAS probe/collector as run) | up to 3–4 |
| After DISC-A22 (shared hub; `quote_source=shared_hub` default) | **1** |
| After DISC-A18 merged onto A22 (promoted depth via hub) | **1** |

Hub transport: Unix domain sockets under `data/fyers/shared_hub/` (control +
stream). Chosen over SQLite WAL for push fan-out and sub-250 ms local delivery on
a single VM. Health: `data/fyers/shared_hub/status.json` reports
`data_socket_owner`, `data_socket_count` (must be 1), and per-owner subscriber
counts.

## Follow-up (not DISC-A15)

`paper_session.py` calls `pipeline.run_once` and `feed.fetch_quotes` every cycle
on top of the timer fetch, which can 429 the following-week chain. Dedupe in a
later slice.

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
