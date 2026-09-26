# Operations and Recovery Runbook

All remediation is bounded deterministic code or an explicit operator command.
AI may summarize evidence but cannot invoke actions.

## Readiness levels

- `LIVE_SAFE`: every confirmed position is protected and reconcilable.
- `ENTRY_READY`: required feeds/features, broker state, risk, storage, clock and
  config are valid for new exposure.
- `RESEARCH_READY`: analytics and model dependencies are available.

Research failure does not remove live safety. Entry readiness is evaluated per
strategy dependency, not one process heartbeat.

## Startup/restart

1. Enter `STARTING`, validate environment/account/config/checksum and clock.
2. Start durable storage and broker connectivity.
3. Enter `RECOVERY`; rebuild local expected state from durable events.
4. Query broker orders, trades/fills, positions and funds.
5. Classify/repair discrepancies and rebuild protective coverage.
6. Validate data/feature freshness and dependent service health.
7. Enter `READY` only when all critical checks pass; otherwise `DEGRADED` or
   `HALTED` with entries blocked.

## Feed stale or disconnected

- Mark dependent snapshots invalid and block affected entries.
- Keep exits using verified broker state and an approved alternate-price policy.
- Reconnect with bounded backoff, rewarm calculations, then restore readiness.

## Broker disconnect

- Stop new submissions and retain correlation/state.
- Continue only controls whose broker semantics are known safe.
- Reconnect, query broker truth and reconcile before retry/amend/new entry.

## Unknown order outcome

- Move order to `UNKNOWN`; freeze conflicting commands/reservation.
- Query by broker/client identity and reconcile fills/position.
- Do not create a replacement until resolved or an explicit audited repair policy
  permits it.

## Partial multi-leg fill

- Update confirmed exposure and capital.
- Invoke the strategy's pre-approved hedge/unwind/timeout policy.
- Block new strategy exposure until repaired.
- Never ask AI to choose the repair.

## Position mismatch

- Freeze affected account/strategy/instrument scope.
- Query broker orders/fills/positions/funds.
- Broker wins external position truth; rebuild local state with imported evidence.
- Restore protection and record ReconciliationEvent before entries resume.

## Safety controls

- Entry freeze: prevent new exposure; continue management/exits.
- Strategy halt: freeze one strategy's new intents.
- Cancel pending: cancel eligible working orders and reconcile races/fills.
- Controlled flatten: close confirmed positions using approved order policy.
- Global halt: block all new exposure and require recovery checks.

Each action records actor, scope, trigger, old/new state, command/result, broker
reference and incident ID.

## Minimum monitoring

Feed/feature age and gaps, clock drift, process heartbeat, storage durability,
broker connection, order unknown/reject/partial counts, reconciliation lag,
exposure/margin/P&L discrepancy, protective coverage and AI cost/schema failures.

Critical alerts require owner, severity and tested runbook. Keep IDs/symbols out
of metric labels and in structured logs/traces.

## Monday PAPER session

Human steps that cannot be coded: put Fyers and Telegram credentials in `.env`,
run `trading data backfill instruments` on Sunday. `fno-automated.service`
supervises the PAPER stack: at PRE_MARKET it verifies Fyers auth (Telegram OAuth
when the token is stale) and starts `fno-data-tick` and `fno-paper-session`.
After you paste the redirect URL once, the session runs unattended.

Operator Telegram alerts (`A2A_TELEGRAM_BOT_TOKEN`, `A2A_TELEGRAM_CHAT_ID` in
`.env`) fire when autopilot cannot keep PAPER services running, Fyers auth fails
at PRE_MARKET, the protection watchdog is stale with open positions, or
`fno-paper-session.service` exits (`OnFailure` → `fno-paper-alert.service`).
Repeats for the same fault are suppressed for 15 minutes.

- Config: `--config config/paper.yaml` (default). Do not point this process at
  LIVE `base.yaml`.
- Session window: 09:15–15:30 IST from `config/data_pipeline.yaml`. EOD Telegram
  plus `data/paper/cohorts/*.json` around 15:40 IST.
- Live Fyers is data and OAuth only. Orders go to the paper broker.
- Missing or stale event-risk blocks new entries. Existing paper positions keep
  deterministic exits.
- Restart reuses `data/paper/trading.sqlite` and `data/paper/broker_state.json`
  so the same idempotency key does not double-submit.
- Net expectancy on the EOD card stays unknown until
  `charges_per_lot.verified_at` is set. That is not required for paper fills.

## Four-mode PAPER routing (2026-09-25)

- Active session config: `config/paper_session.yaml` with `routing_profile: four_mode`.
- **DISCOVERY profile:** set `entry_profile: DISCOVERY` in
  `config/paper_session.yaml` (default when absent is `STRICT`). Under DISCOVERY,
  `experiment_prefix` is `EXP-DISC`, straddle/strangle families run `PAPER`, and
  soft gates record `strict_would_block` instead of blocking. Startup prints
  `ENTRY PROFILE: DISCOVERY (temporary)`; the heartbeat includes
  `entry_profile: DISCOVERY`. See `docs/context/DISCOVERY_MODE.md`.
- **Rollback DISCOVERY:** set `entry_profile: STRICT` (or remove the key) in
  `config/paper_session.yaml`, then
  `sudo systemctl restart fno-paper-session.service`. Positions opened under
  DISCOVERY keep their frozen exit policy after rollback; only new entries
  revert to STRICT rules.
- Rollback to legacy one-winner router:
  `cp config/paper_session_legacy.yaml config/paper_session.yaml` then
  `sudo systemctl restart fno-paper-session.service`.
- Four-mode file (reference): `config/paper_session_four_mode.yaml`.
- Mode policy: `config/modes.yaml`. M2 capital share is 28% and its per-trade
  fraction is 4% (cap ₹7,840 on the ₹7L book). M1 is 5%, M3 is 2%, M4 is 1%.
- Startup log line `Loaded mode stances after startup validation` is the loaded
  stance. `M1_CAS: PAPER` stays PAPER even on a 60-second poll. A missing or
  failing oracle-measured latency report emits a warning only; latency is a
  measured PAPER limitation, not a promotion gate. M1 entries use
  `submit_m1_event`; the poll clears pending events and does not submit M1.
- Latency thresholds (chosen before measurement): quote age 500 ms, decision
  2000 ms, execution 2000 ms, exit gap 2000 ms. Samples go to
  `data/paper/m1_latency_report.json` when provider timestamps exist.
- M1 scan windows are 09:20-15:00 and 15:00-15:25 IST. The cash auction
  15:30-15:40 is not an option entry window.
- M2 flattens at 15:20 IST unless the carry gate records `CARRY_APPROVED`.
  Carry keeps the position in M2.
- Pre-open checks on Oracle:
  `systemctl is-active fno-automated.service fno-data-tick.service`;
  `uv run pytest tests/test_four_mode_session_integration.py tests/test_cas_event_path.py -q`;
  entry freeze query on `data/paper/trading.sqlite` (no row or `entries_blocked=0`).
- A session with only abstentions is not an observed fill. Each abstention needs
  a reason code in the cycle evidence.

