# Defined-Risk Credit Spread

STRATEGY_ID: defined_risk_multileg
VERSION: defined-risk-multileg-v1
STATUS: RESEARCH

## Hypothesis

A covered bull-put or bear-call spread may express a directional view with capped
loss and positive initial credit. Current strike inputs, stop and target are paper
hypotheses; Layer 3 does not yet rank a complete option chain.

## Paper requirements

Require matching underlying/type/expiry, ordered protective wing, known OI,
bounded quotes, confirmed margin and clear event-risk state. Recompute strike-width
loss minus credit and all charges in Layer 2. Exercise partial-fill repair and
report tail loss, margin movement, fill quality and net expectancy by regime.
