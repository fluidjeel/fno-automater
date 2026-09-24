# Paper trading dashboard and supervision — resume checklist

## Current state

- Local dashboard exists and can inspect the Oracle host read-only.
- Oracle supervisor (`fno-automated.service`) starts and recovers PAPER units by phase.
- `fno-paper-session.service` is installed; the supervisor starts it at PRE_MARKET.
- Protection code now imports, writes session heartbeat storage, persists degraded state, and restores the protection freeze during restart recovery.
- Live trading remains untouched; these changes are scoped to the PAPER path.

## Remaining work

- [x] Rerun `tests/test_paper_protection.py` / safety hardening; fixed protection recovery so fresh quotes clear `entries_blocked`.
- [x] Run targeted paper protection/session/lifecycle/runner suites (green).
- [x] Add durable portfolio Greeks / scenario-P&L snapshot storage and emit snapshots from the paper session.
- [x] Expose portfolio-risk telemetry in the dashboard collector and coverage view.
- [x] Add and validate `fno-paper-session.service` deployment files.
- [x] Sync the verified code to Oracle.
- [x] Install the paper service on Oracle; supervisor autopilot starts it at PRE_MARKET.
- [x] Verify supervisor, heartbeat, journal, and dashboard evidence from Oracle.
- [x] Autopilot: operator action only for credential setup and Telegram OAuth redirect.

## Safety boundary

Do not enable live trading as part of this task. Live execution still requires separate broker-resident protection, reconciliation, kill-switch, and deployment review.
