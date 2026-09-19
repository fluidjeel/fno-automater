# Directional Commodity Futures

STRATEGY_ID: commodity_futures_trend
VERSION: commodity-futures-v1
STATUS: RESEARCH

## Hypothesis

A material close-relative commodity move may persist over the configured holding
horizon. Current signal threshold and stop/target distances are paper hypotheses.

## Paper requirements

Require a verified future contract, known OI, valid quote, bounded spread, DTE,
fresh event-risk state and confirmed margin preview. Measure gap-through-stop
loss, point-value and lot math, margin changes, slippage, costs, drawdown and tail
loss separately for long and short trades. Margin is collateral, not maximum loss.
