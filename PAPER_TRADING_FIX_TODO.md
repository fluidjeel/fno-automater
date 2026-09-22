# Paper trading dashboard and supervision — resume checklist

## Current state

- Local dashboard exists and can inspect the Oracle host read-only.
- Oracle currently has the supervisor unit enabled but inactive.
- `fno-paper-session.service` is not installed on Oracle.
- Protection code now imports, writes session heartbeat storage, persists degraded state, and restores the protection freeze during restart recovery.
- Live trading remains untouched; these changes are scoped to the PAPER path.

## Remaining work

- [x] Rerun `tests/test_paper_protection.py` / safety hardening; fixed protection recovery so fresh quotes clear `entries_blocked`.
- [x] Run targeted paper protection/session/lifecycle/runner suites (green).
- [ ] Add durable portfolio Greeks / scenario-P&L snapshot storage and emit snapshots from the paper session.
- [ ] Expose portfolio-risk telemetry in the dashboard collector and coverage view.
- [ ] Add and validate `fno-paper-session.service` deployment files.
- [ ] Sync the verified code to Oracle.
- [ ] Install the paper service on Oracle, but do not start it until paper credentials/configuration are confirmed.
- [ ] Verify supervisor, heartbeat, journal, and dashboard evidence from Oracle.
- [ ] Only after explicit confirmation, start supervised PAPER execution.

## Safety boundary

Do not enable live trading as part of this task. Live execution still requires separate broker-resident protection, reconciliation, kill-switch, and deployment review.
