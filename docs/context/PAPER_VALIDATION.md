# Forward Paper Validation Standard

The owner has chosen live-market paper testing instead of historical backtesting
for the current milestone. This is a valid forward test, but it observes only
the regimes and failures that occur during the paper window. It cannot establish
historical robustness or remove the need for conservative live promotion.

Layer 4 scores frozen experiments; it does not promote. Process `Environment`
stays `PAPER`/`LIVE`. Per-trade `ExecutionMode` is `SHADOW` → `PAPER` →
`CANARY_REAL` → `LIMITED_REAL` → `NORMAL_REAL` (or `SUSPENDED`). Real-capital
modes require `Environment.LIVE` and verified LIVE config.

## Experiment freeze

An evidence cohort is one `ExperimentDefinition`: strategy version, parameter
version, feature-set version, risk-policy version, fill-model version, code
version and execution mode, with `parameters_frozen=true` after start. A
parameter or fill-model change starts a new `experiment_id`. Never pool
pre-change and post-change results when judging promotion.

A position opened under version V keeps V's exit policy. New config applies only
to new trades. Intents and orders carry `experiment_id` and `execution_mode`.

## Conservative fills

Promotion scorecards re-price recorded intents through the conservative fill
calculator (`trading.analytics.fills`), not last-trade-at-limit fantasy fills.
The live paper broker fills immediately at limit unless constructed with the
same `fill_model` (`PAPER-002`).

Starter rules (versioned in `config/evaluation.yaml`): long entry at ask plus
slippage, long exit at bid minus slippage, LIMIT only if the market traded
through, no fill if displayed depth is below quantity, multi-leg later legs may
move adversely. Unverified brokerage/STT/GST fail closed in money fields.

## Evidence cohort

For every strategy evaluation retain the decision time, all input snapshot IDs,
data quality and lineage, event-risk state, emitted intent or abstention/rejection,
Layer 2 result, executable quotes, paper-order lifecycle, reconstructed fill,
charges, exit and reconciliation outcome. Retain declined signals so selection
behavior is visible.

## Report

The deterministic scorecard reports sample size, market exposure, gross and net
P&L, expectancy, payoff, turnover, drawdown, MAE/MFE, quoted versus realized
slippage, fill/partial/reject rates, risk-gateway reason histogram, expiry and
session buckets, capital/margin utilisation and incident counts. Show results by
strategy and cohort; do not present a blended aggregate as evidence for every
strategy.

`trading evaluate scorecard <cohort>` and `trading evaluate eligibility <cohort>`
are read-only. They cannot write live config.

## Required drills

- stale, missing and contradictory data;
- missing OI/depth and excessive spread;
- active, stale, wrong-scope and unavailable event-risk state;
- disconnect/reconnect and startup reconciliation;
- unknown submit outcome and cancel/fill race;
- partial multi-leg fill and failed repair;
- missing protective coverage, daily-loss kill switch and controlled halt.

## Promotion

Thresholds are frozen in `config/evaluation.yaml` before scoring (window length,
min signals, min closed trades, expectancy after costs, drawdown cap, no
unresolved CRITICAL/WARNING incidents, fill/slippage bounds, not one-trade
concentrated, more than one regime tag if present, daily reconciliation
complete). Win rate / gross P&L alone never pass.

`PromotionEligibilityResult` is `ELIGIBLE`, `INELIGIBLE` or `INSUFFICIENT_SAMPLE`.
An `ELIGIBLE` result on an offline fixture is a pipeline test, not a go-live.
Paper completion never changes LIVE config or credentials automatically.
Promotion requires a reviewed record that names the exact cohort, enabled
strategies, minimal capital, rollback thresholds and rollback target.
