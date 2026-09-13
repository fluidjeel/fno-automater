# Broker Adapter and Reconciliation Specification

## Boundary

The adapter translates canonical commands and broker responses. It contains no
strategy selection, sizing, stop policy, retry policy that creates duplicate
risk, or AI calls.

Define typed ports for:

- Account/funds/margin query.
- Order submit, amend and cancel.
- Order/trade/fill/position/holding query.
- Order updates or polling fallback.
- Market/session status where supported.
- Instrument/order capability discovery where needed.

## Broker facts to verify

Before implementing an adapter, verify current official documentation and paper
behavior for authentication/refresh, client order IDs, idempotency support, order
states, timestamps, partial fills, modifications, cancellation races, rate limits,
reconnect, position fields, multi-leg support and error taxonomy.

Never hard-code remembered exchange/broker thresholds in domain logic.

## Canonical command requirements

- Correlation, intent, risk-decision and idempotency IDs.
- Account/environment identity.
- Instrument/contract identity, side, quantity, order type, price/trigger and
  validity.
- Strategy/algo tags when currently required and verified.
- Command creation and expiry time.

## Submission behavior

1. Confirm system and strategy are entry-ready.
2. Confirm RiskDecision is valid and unexpired.
3. Persist order intent and capital reservation.
4. Submit once with stable client/idempotency identity.
5. Persist raw response and canonical transition.
6. Resolve timeout/unknown state by query/reconciliation, never blind retry.
7. Drive trade/portfolio state from confirmed broker events.

## Reconciliation

Run on startup, reconnect, scheduled interval, manual request and anomaly.
Compare broker orders, fills/trades, positions and funds with local expected state.

Classify differences:

- Timing lag: wait bounded interval and re-query.
- Known mapping/normalization issue: repair derived local representation.
- Unknown/missing local event: import broker evidence and freeze affected scope.
- Unexpected broker order/position: critical alert and approved deterministic
  cancel/flatten/recovery policy.
- Local order absent at broker: mark unresolved; do not assume rejection/fill.

Entries remain blocked until critical discrepancies and protective coverage are
resolved. Every comparison and repair creates a ReconciliationEvent.

## Failure expectations

- Rate limit: bounded queue/backoff; prioritize risk-reducing commands.
- Disconnect: stop new submissions, reconnect, then reconcile.
- Partial fill: update reservation/exposure and follow approved leg policy.
- Reject: preserve reason, release eligible reservation, do not transform order
  type automatically unless policy explicitly allows it.
- Amend/cancel race: broker state wins; reconcile fill before further command.
- Clock/auth/account mismatch: halt affected environment.

