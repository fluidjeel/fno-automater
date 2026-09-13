# Strategy Specification Template

STRATEGY_ID:
VERSION:
STATUS: RESEARCH | SHADOW | PAPER | LIVE | RETIRED
OWNER:

## Objective and hypothesis

State the market behavior, why it may persist, and expected holding period.

## Instruments and universe

Allowed underlying/products/contracts, exclusions, expiry/roll selection and
point-in-time eligibility.

## Required data

Fields, sources, feature versions, timeframe, warm-up, freshness and valid quality
states. Define behavior for missing/degraded data.

## Decision schedule

Exchange timezone, evaluation frequency/window, entry cutoff, holding and expiry
constraints.

## Deterministic eligibility and entry

Exact boolean/numeric conditions. Separate candidate setup, confirmation and
Trade Intent emission. No AI call or broker state mutation.

## Structure selection

Allowed legs/templates, strike/expiry selection, liquidity/spread/OI checks,
defined max-loss calculation and legging constraints.

## Trade Intent

Required direction, trigger/limit policy, requested risk, invalidation, maximum
holding, slippage/liquidity constraints and idempotency scope.

## Deterministic exit template

Initial stop, target, break-even, trailing, time, expiry, event and emergency
rules. State monotonic stop behavior.

## Layer 2 limits

Strategy/instrument/exposure/concentration/correlation/daily limits and what
Layer 2 must independently recalculate.

## Partial-fill and recovery policy

Single/multi-leg timeout, hedge/unwind, restart/reconciliation and stale-data
behavior. No discretionary AI decision.

## Backtest and validation

Point-in-time inputs, execution/cost model, walk-forward design, holdout, regime
slices, capacity, metrics, uncertainty and minimum operational evidence.

## Promotion and rollback

Shadow/paper/minimal-capital gates, signed config version, health triggers,
rollback conditions and retirement.

## Known failure modes

List structural, data, liquidity, execution, model and operational risks.

