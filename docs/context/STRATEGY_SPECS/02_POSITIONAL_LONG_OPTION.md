# Positional Long Option

STRATEGY_ID: positional_long_option
VERSION: long-option-v1
STATUS: RESEARCH

## Hypothesis

A close-relative directional move, optionally overridden by fresh promoted macro
evidence, may persist long enough to justify buying a liquid call or put. The
current threshold, contract filters, stop and target are paper hypotheses.

## Paper requirements

Require valid underlying and option snapshots, known OI above the configured
minimum, bounded spread, sufficient DTE and clear event-risk state. Measure
premium decay, IV changes, adverse/favorable excursion, fill quality, costs and
net expectancy by volatility and expiry regime under `PAPER_VALIDATION.md`.

Promotion requires stable cohort evidence after costs and successful protection,
reconciliation and failure drills. Layer 2 recalculates premium risk and size.
