# POC Strategy: Positional Defined-Risk Index Options

STRATEGY_ID: positional_index_options_poc
VERSION: 0.1-draft
STATUS: RESEARCH

This is an architecture-ready specification, not a validated trading edge.
Parameters remain unset until data research and broker constraints are verified.

## Objective

Express a weekly/positional index regime using defined-risk option structures.
The macro AI may propose bounded regime/strategy-family context before the week;
deterministic rules own eligibility, structure, entry and all exits.

## Instruments

- Initial underlying: one liquid NSE index selected during planning.
- Allowed structures: debit spread, defined-risk credit spread, iron condor and
  calendar only after each has a validated deterministic builder.
- No naked short options. Directional outright options are premium-paid only.

## Data requirements

- Underlying OHLCV and current quote.
- Option chain with contract identity, expiry, strike, bid/ask, OI/volume and
  timestamps.
- IV/Greeks with model/input/version/validity.
- Trading calendar, expiry, lot/tick size and verified broker margin preview.
- Approved regime/volatility features and optional promoted weekly macro proposal.

Freshness, warm-up and liquidity thresholds are configured per timeframe and must
be validated. Invalid/stale required data means no new intent.

## Decision model

1. Deterministic universe/contract/data-quality filter.
2. Read only a valid, unexpired, promoted macro/regime proposal; otherwise use
   approved deterministic baseline or abstain.
3. Deterministic technical/regime conditions select from approved structure enums.
4. Structure builder enumerates valid contracts and computes payoff/max loss.
5. Strategy emits Trade Intent with requested risk and complete exit template.
6. Layer 2 recalculates max loss, size, margin, portfolio fit and executable price.

No immediate low-latency execution is required, but orders still use current
quotes, slippage constraints, expiry, idempotency and reconciliation.

## Entry and structure parameters to research

- Decision weekday/time and minimum days to expiry.
- Trend/range and realized/implied volatility regimes.
- IV percentile, skew/term structure and event blackout.
- Strike selection by delta/distance/payoff.
- Minimum bid/ask/OI/volume and maximum spread/slippage.
- Maximum entry attempts and order timeout.

Do not invent defaults for production. Research configuration must identify every
trial and preserve a final holdout.

## Exit template requirements

- Defined maximum loss and underlying/structure invalidation.
- Deterministic stop and profit target.
- Time/expiry exit and event-risk handling.
- Break-even/trailing adjustment only if pre-specified and monotonic.
- Multi-leg partial-fill/repair policy.
- Protective management independent of AI availability.

## Validation

- Point-in-time option chain and contract reference data.
- Realistic bid/ask execution, brokerage/taxes/fees, slippage and rejected/partial
  fills.
- Walk-forward and regime/event slices; no final-holdout tuning.
- Compare structure candidates on tail loss, drawdown, expectancy, capacity,
  stability and execution quality, not win rate alone.
- Shadow then paper across normal, volatile, expiry and broker-failure conditions.

## Promotion blockers

- Unverified contract/margin/order semantics.
- Missing deterministic max-loss or partial-fill recovery.
- Any AI-to-order dependency.
- Replay mismatch, stale-data acceptance, unprotected open position, unresolved
  reconciliation or failed kill-switch drill.

