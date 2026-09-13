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

### Replay/research

Decision-time visibility, costs, spread, slippage, latency, liquidity, contract
roll, expiry, corporate actions, point-in-time universe, walk-forward and regime
slices. Same snapshot/config/code version reproduces the same decision.

## Backtest evidence

Report sample size and exposure time, return/CAGR, drawdown magnitude/duration,
volatility/downside/tail loss, expectancy, payoff, turnover, costs, capacity,
regime stability and out-of-sample performance. Separate strategy, sizing and
execution contributions.

No tuning on the final holdout. Record training/selection/evaluation windows and
all parameter trials needed to interpret selection bias.

## Release ladder

1. Static/type/unit/invariant checks.
2. Contract and failure scenarios.
3. Historical deterministic replay.
4. Live-data shadow decisions with no orders.
5. Paper execution and broker recovery drills.
6. Minimal live capital only after explicit promotion.
7. Gradual scaling with rollback triggers.

## Blocking gates

Do not promote while any critical data-freshness, risk, idempotency, order-state,
reconciliation, protective-exit, audit, restart or kill-switch test fails.

Profitability or win rate alone is never sufficient. Promotion criteria are
versioned per strategy and include drawdown/tail risk, execution quality,
stability, sample size, operational health and failure recovery.

## Change-specific verification

- Contract change: producer/consumer tests and migration/compatibility.
- Calculation change: golden/replay comparison and numeric boundary tests.
- Strategy change: leakage checks, out-of-sample evidence and intent parity.
- Risk/execution change: invariant plus failure scenarios and broker fixtures.
- AI change: schema, grounding, injection, abstention, timeout and cost tests.
- Operations change: restart/reconcile/rollback drill.

