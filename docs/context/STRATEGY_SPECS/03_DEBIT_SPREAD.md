# Vertical Debit Spread

STRATEGY_ID: debit_spread
VERSION: debit-spread-v1
STATUS: RESEARCH

## Hypothesis

A defined-risk bull-call or bear-put spread can express a directional move with
less premium and vega exposure than an outright option. Current strike input,
thresholds and exits are paper hypotheses; Layer 3 does not yet rank a full chain.

## Paper requirements

Both legs require matching underlying/type/expiry, distinct ordered strikes,
known OI, valid quotes, bounded spreads and a clear event-risk state. Report net
debit, capped payoff, legging/fill behavior, costs, drawdown and net expectancy by
expiry and volatility regime. Layer 2 independently recomputes debit and size.
