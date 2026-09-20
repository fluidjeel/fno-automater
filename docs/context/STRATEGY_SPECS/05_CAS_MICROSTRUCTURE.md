# Close-Auction Microstructure

STRATEGY_ID: cas_microstructure
VERSION: cas-microstructure-v1
STATUS: PAPER

## Hypothesis

Aligned auction imbalance, trade-flow imbalance and microprice edge near the
close may persist briefly enough for a premium-paid directional option trade.

## Blocker and paper requirements

Layer 1 computes `cas-microstructure-v1` from timestamped depth (and ticks when
present). A missing prior depth omits flow/instability keys; the strategy fails
closed on `DATA_GAP` rather than defaulting. PAPER may submit when session
stance is `PAPER`, P0 is valid, P1 depth is observed, and the 15:00–15:30 IST
window is open. LIVE stays off. Constructed unit-test features are not
promotion evidence; measure fillability, quote instability, decay, adverse
excursion and forced time-exit quality on live depth.
