# Domain Contracts

Implement these as strict versioned models. Names may adapt to an established
codebase, but semantics and authority must remain.

## Common rules

- UTC storage plus exchange-local timestamp where relevant.
- Stable globally unique IDs and correlation IDs.
- Explicit schema/config/code versions.
- `Decimal` or integer minor units/ticks for accounting/order validation.
- Reject unknown fields, NaN/infinity, naive time, invalid precision and unknown
  instruments.
- Serialize and replay without information loss.

## FeatureSnapshot

Required groups:

- Identity: `snapshot_id`, schema version, instrument/contract identity.
- Time: event, source, receive and calculation timestamps.
- Market: bid/ask/last, OHLCV, depth/option-chain summary as applicable.
- Derivatives: expiry, strike, option type, IV, Greeks, skew, OI, liquidity.
- Features: named feature-set version and values.
- Quality: state, freshness/age, gaps, warm-up, source status, reason codes.
- Lineage: provider/source IDs, raw event offsets/references, config/code version.

## AIProposal

- `proposal_id`, schema version, proposal type and affected scope.
- As-of time and hard expiry.
- Model, prompt, retrieval/data and policy versions.
- Allowlisted evidence references and retrieval times.
- Bounded recommendation enum and calibrated confidence metadata.
- Assumptions, contradictions, missing-data flags and `ABSTAIN` support.
- Explanatory text separated from machine-consumable fields.

AIProposal is stored for research/audit. It is never a broker instruction.

## TradeIntent

- `intent_id`, correlation ID, strategy ID/version.
- Snapshot and optional promoted-proposal/config references.
- Underlying/product/contract, direction and all legs.
- Trigger and limit policy; acceptable spread/slippage and timeout.
- Requested risk, strategy-estimated max loss and invalidation.
- Exit template: stop, target, break-even, trailing, time and expiry rules.
- Session, liquidity, event-blackout and holding constraints.
- Created/expiry timestamps and stable idempotency key.

TradeIntent expresses desired exposure, never broker command or final quantity.

## RiskDecision

- Intent/correlation IDs and policy/config versions.
- Decision: `APPROVE`, `RESIZE`, `DEFER`, `REJECT`.
- Approved quantity/legs and capital reservation ID.
- Pre-trade exposure and projected post-fill portfolio state.
- Recalculated max loss, margin and liquidity assessment.
- Applied limits and machine-readable reason codes.
- Decision time and expiry. Expired approval must be recalculated.

## OrderEvent

- Internal/client/broker order IDs and idempotency key.
- Intent, risk decision, trade and correlation IDs.
- Attempt number, command type, quantity, price and order parameters.
- Requested, acknowledged and filled quantities/prices.
- Local send/receive and broker timestamps.
- Canonical state plus raw broker status/payload reference.
- Error/rejection reason and reconciliation status.

## ReconciliationEvent

- Scope and trigger: boot, reconnect, scheduled or anomaly.
- Expected local and observed broker state references.
- Difference classification and severity.
- Deterministic repair action, result and resolution timestamp.

## Minimum state machines

- System: `STARTING -> RECOVERY -> READY -> DEGRADED -> HALTED`.
- Intent: `CREATED -> VALIDATED -> APPROVED|RESIZED|DEFERRED|REJECTED|EXPIRED`.
- Order: `CREATED -> SUBMITTING -> ACKNOWLEDGED -> PARTIAL -> FILLED`; explicit
  `REJECTED`, `CANCEL_PENDING`, `CANCELLED`, `EXPIRED`, `UNKNOWN`.
- Trade: `PENDING_ENTRY -> OPENING -> OPEN -> EXIT_PENDING -> CLOSING -> CLOSED`;
  explicit `REPAIR_REQUIRED`.

Illegal transitions fail closed and emit audit evidence.

