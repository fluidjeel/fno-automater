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
run `trading data backfill instruments` on Sunday, then start
`trading paper session` (tmux/systemd) before the open. The process sends the
Fyers login URL on Telegram; after you paste the redirect it runs unattended.

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


