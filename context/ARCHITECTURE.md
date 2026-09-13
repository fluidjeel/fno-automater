# Four-Layer Architecture

## Control flow

```text
External data -> Layer 1 FeatureSnapshot -> Layer 3 TradeIntent
              -> Layer 2 RiskDecision/OMS -> Broker

Broker events -> Layer 2 reconciliation/portfolio/trade management
Audit evidence -> Layer 4 evaluation -> candidate proposal -> promotion gate
```

No arrow goes directly from AI or a strategy to the broker.

## Layer 1: Core Foundations and Data

Responsibilities:

- Feed adapters for ticks, OHLCV, option chain, OI, licensed L2/depth, macro and
  events.
- Instrument master, contract calendar, expiries, lot/tick sizes, sessions and
  corporate actions.
- Normalization, de-duplication, ordering, timestamping and corrections.
- Data-quality states: `VALID`, `DEGRADED`, `STALE`, `INVALID` with reason codes.
- Reusable deterministic features: bars, returns, indicators, realized
  volatility, IV, Greeks, skew, term structure, liquidity and OFI/MLOFI.
- Immutable raw/normalized events, versioned snapshots and deterministic replay.

Layer 1 does not select trades, size positions, or apply portfolio policy.

## Layer 2: Common Trading, Risk and State

Exclusive live authority:

- Broker reconciliation and confirmed portfolio view.
- Capital reservation, sizing and allocation.
- Per-trade/strategy/instrument/asset/day/portfolio limits.
- Exposure, concentration, correlation, margin and liquidity checks.
- Trade lifecycle and deterministic stop/target/trailing/time/expiry exits.
- OMS submission, amendment, cancellation, fill handling and rate limiting.
- Entry freeze, strategy halt, cancel pending, controlled flatten and recovery.

Input: typed Trade Intent. Output: `APPROVE`, `RESIZE`, `DEFER`, or `REJECT`.
Layer 2 recalculates all financially authoritative values at decision time.

## Layer 3: Strategy Systems

Independent pure strategy plug-ins consume an immutable Feature Snapshot and a
read-only Portfolio View, then emit zero or more Trade Intents.

Strategy systems:

1. Positional stock/index option structures.
2. Directional premium-paid stock/index options.
3. CAS/microstructure.
4. Positional commodities.
5. Directional commodity futures.

Each plug-in owns its setup logic and requested risk, but not final quantity,
portfolio acceptance, broker execution, or protective lifecycle.

## Layer 4: Analytics and Self-Improvement

Asynchronous components:

- Weekly macro/regime strategist using bounded enums and grounded evidence.
- Universe ranking after deterministic eligibility/liquidity filtering.
- Post-market evaluator and failure clustering.
- Parameter/strategy hypotheses.
- Drift, calibration, execution-quality and data-quality analytics.
- Backtest, replay, walk-forward, shadow and paper evaluation.

Output is an evidence package. It cannot mutate live configuration. Promotion:

```text
proposal -> schema validation -> deterministic evaluation -> risk tests
         -> shadow/paper -> approval record -> signed config -> deployment
```

## Cross-cutting pillars

- Observability: metrics, structured logs, traces, heartbeat and audit.
- Remediation: bounded retries, circuit breakers, recovery state and runbooks.
- Security: least privilege, separated environments, secret management.
- Governance: versioned contracts/config, promotion, rollback and lineage.

## Runtime placement

Persistent VPS/process group:

- Active feed adapters and Layer 1 calculations.
- Layer 2 portfolio/risk/OMS/reconciliation/exits/safety.
- Active Layer 3 strategies.
- Durable events and live-safe observability.

Asynchronous scheduled/serverless:

- Macro/news aggregation and weekly proposal.
- Post-market evaluator and reports.
- Parameter research and large backtests when suitable.

The persistent core cannot require a cross-cloud or LLM call to remain live-safe.

## Dependency rules

- Domain and calculations import no infrastructure.
- Adapters implement application/domain ports.
- Strategies depend on domain contracts, not OMS/broker adapters.
- Analytics may read durable evidence but cannot write live state.
- Dashboard/read models are derived and disposable.

