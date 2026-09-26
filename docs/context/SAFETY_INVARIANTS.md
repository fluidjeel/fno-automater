# Safety Invariants

These requirements override convenience, strategy behavior, and AI output.

## Authority

1. Deterministic versioned code exclusively controls live signals, sizing,
   allocation, risk, order creation, stops, exits and flattening.
2. AI cannot call broker tools, mutate live state/config, or bypass any gate.
3. Strategies cannot submit broker orders; they emit Trade Intents only.
4. Layer 2 independently validates every authoritative financial value.
5. Broker-reported orders, fills, positions and funds are external truth.

## Fail-closed behavior

6. Unknown, stale, inconsistent or invalid critical state blocks new exposure.
7. Missing/invalid/late AI output uses an approved deterministic baseline or no
   new exposure.
8. Existing positions retain deterministic protective management without AI.
9. Startup/restart/reconnect begins in `RECOVERY`; entries wait for reconciliation.
10. Storage uncertainty or excessive clock drift blocks time-sensitive entries.

## Orders and capital

11. One logical order uses one stable idempotency key across retries.
12. Timeout/acknowledgement never proves a fill.
13. Unknown submit outcome blocks replacement until broker reconciliation.
14. Capital is reserved before submission and released/adjusted from confirmed
    events.
15. Partial fills and multi-leg failures use a pre-approved hedge/unwind policy.
16. Every open position maps to active deterministic protective coverage.
17. Stops never widen after entry unless a versioned tested policy explicitly
    authorizes the transition.

## Data and replay

18. Decisions reference one immutable versioned snapshot; mixed timestamps are
    never hidden.
19. Critical values include freshness, validity, unit, time and lineage.
20. Replay/backtest contains no lookahead, survivorship or future-data leakage.
21. Same snapshot + config + code version produces the same decision.

## Change control

22. AI/evaluator output is a proposal, never a deployment.
23. Live config changes require schema validation, evidence, approval record,
    version/checksum, deployment record and rollback target.
24. Kill-switch actions are deterministic, independently callable and tested.
25. Every decision/order/recovery transition is durably auditable.

Any code review touching these invariants requires explicit failure tests.

## PAPER discovery exception (temporary)

When the PAPER entry profile is `DISCOVERY` (`DISCOVERY_MODE.md`), invariant 6
narrows. "Critical state" then means only:

- price presence and quote age;
- instrument identity;
- recovery and reconciliation;
- storage and order-outcome state.

Other quality and policy checks are evaluated and recorded as
`strict_would_block`; they do not block new PAPER exposure. All other
invariants apply unchanged. `DISCOVERY` must fail validation under
`Environment.LIVE`. The target remains the `STRICT` profile.

