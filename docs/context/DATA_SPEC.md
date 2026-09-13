# Data and Calculation Specification

## Required data classes

- Live market: trades/ticks, quotes, OHLCV, option chain, OI and licensed depth.
- Reference: symbol mapping, contracts, expiries, strikes, lot/tick sizes, session,
  price bands and corporate actions.
- Macro/event: rates, FX/DXY proxies, volatility, commodities, economic calendar
  and approved timestamped news evidence.
- Broker: orders, trades/fills, positions, holdings, funds/margin and market status.

Provider selection remains configuration. Verify entitlements, timestamps,
reconnect behavior, correction semantics and rate limits before implementation.

## Canonical event requirements

- Provider/source and instrument/contract identity.
- Event/source/receive times and provider sequence where available.
- Explicit units and price/quantity precision.
- Raw payload reference/hash and normalization version.
- Correction/cancel semantics; duplicate and ordering state.

## Quality gate

Assign `VALID`, `DEGRADED`, `STALE`, or `INVALID` with reason codes based on
strategy-specific thresholds for freshness, gaps, sequence, cross-source sanity,
clock drift, session and warm-up. Do not use one universal freshness threshold.

## Calculation ownership

- Layer 1: bars, returns, indicators, realized volatility, IV/Greeks, term
  structure/skew, liquidity, OFI/MLOFI and reusable regime features.
- Layer 3: setup/entry conditions and structure candidate.
- Layer 2: final quantity, money, margin, exposure, limits, stop/target/trail and
  executable price.
- Layer 4: attribution, drift, calibration and candidate tuning evidence.

## Calculation rules

- Event-time processing; provisional and final bars are different states.
- Timezone-aware timestamps; UTC storage and exchange-local session logic.
- Explicit period/annualization, currency, contracts/lots, percent/fraction and
  tick/price units.
- Product-configured pricing convention and curves/carry/dividend assumptions.
- Record input snapshot, model, convergence/validity and calculation version.
- No silent forward-fill for execution-critical data.

## Historical/replay integrity

- Point-in-time universe/reference data and contract rolls.
- Corporate-action adjustment policy.
- No use of completed bar before completion or revised data before publication.
- Realistic costs, spreads, slippage, liquidity and rejection/partial-fill model.
- Replay can reproduce decisions from retained event order and exact versions.

## POC storage

- Preserve immutable raw and canonical data separately.
- DuckDB/Parquet are acceptable for local analytics/replay.
- Live order/risk events require crash-safe durable writes and tested recovery.
- Retention and compaction must preserve audit/replay lineage.

