# Close-Auction Microstructure

STRATEGY_ID: cas_microstructure
VERSION: cas-microstructure-v1
STATUS: RESEARCH

## Hypothesis

Aligned auction imbalance, trade-flow imbalance and microprice edge near the
close may persist briefly enough for a premium-paid directional option trade.

## Blocker and paper requirements

Layer 1 does not yet produce the declared feature contract. Keep this strategy
disabled until `cas-microstructure-v1` is calculated from timestamped depth/trade
events with explicit units, quality, and latency. Then measure fillability in the
short window, quote instability, decay, adverse excursion and forced time-exit
quality. Constructed unit-test features are not promotion evidence.
