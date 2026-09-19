# Testing and Release Standard

## Test classes

### Unit

Pricing inputs/rounding, feature calculations, sizing, limits, stops, schema
validation, state transitions and reason codes.

### Invariant/property

- Same input/version produces same output.
- No risk-limit bypass through rounding or missing fields.
- No duplicate/negative capital reservation.
- Stable idempotency under duplicate processing.
- Stops are monotonic under the approved policy.
- Illegal state transitions are rejected.

### Contract

Recorded/sanitized feed and broker fixtures, storage serialization, OMS adapter,
AI proposal schema and producer/consumer compatibility.

### Scenario/failure

Stale/missing/duplicate/out-of-order events, clock drift, partial fill, reject,
unknown submit timeout, amend/cancel race, rate limit, disconnect, restart, broker
mismatch, storage failure, AI timeout/malformed response and kill switch.

### Replay / incident reconstruction

Decision-time visibility, costs, spread, slippage, latency, liquidity, contract
roll, expiry and corporate actions. Same snapshot/config/code version reproduces
the same decision. Replay is for debugging and reconstruction, not a promotion
Sharpe gate.

## Forward-validation evidence

Report sample size, market exposure, gross and net P&L after conservative costs,
expectancy, payoff, turnover, drawdown magnitude and duration, MAE/MFE, quoted
versus realized slippage, fill/partial/reject rates, risk-gateway reasons,
capacity/margin utilisation and results by volatility, trend/range, expiry and
event state. Separate strategy and cohort; do not present a blended aggregate as
evidence for every strategy.

Cohort key = experiment + fill-model version. Never pool pre-change and
post-change results. Win rate or gross P&L alone is never sufficient.

Offline fixture `ELIGIBLE` results prove the pipeline, not go-live.

## Release ladder

1. Static/type/unit/invariant checks.
2. Contract and failure scenarios.
3. Recorded-session replay (incident reconstruction / debug).
4. Live-data shadow decisions with no orders (`SHADOW`).
5. Paper execution and broker recovery drills (`PAPER`).
6. Minimal live capital only after explicit human-audited promotion
   (`CANARY_REAL`).
7. Gradual scaling with rollback triggers (`LIMITED_REAL` → `NORMAL_REAL`).

There is no historical backtest Sharpe gate on this ladder.

## Blocking gates

Do not promote while any critical data-freshness, risk, idempotency, order-state,
reconciliation, protective-exit, audit, restart or kill-switch test fails.

Profitability or win rate alone is never sufficient. Promotion criteria are
versioned per strategy and include drawdown/tail risk, execution quality,
stability, sample size, operational health and failure recovery.

## Change-specific verification

- Contract change: producer/consumer tests and migration/compatibility.
- Calculation change: golden/replay comparison and numeric boundary tests.
- Strategy change: leakage checks, live-forward cohort evidence and intent parity.
- Risk/execution change: invariant plus failure scenarios and broker fixtures.
- AI change: schema, grounding, injection, abstention, timeout and cost tests.
- Operations change: restart/reconcile/rollback drill.

