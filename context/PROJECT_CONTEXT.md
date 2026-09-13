# Project Context

## Mission

Build a reliable autonomous trading platform for Indian markets in which
deterministic software owns every live decision and AI improves research,
context, evaluation, and candidate configuration outside the live path.

This is an automated trading system with AI support, not an AI trading system.
There is no routine human in the live loop, so the system must remain safe under
feed, broker, process, storage, configuration, and AI failures.

## Initial scope

- Markets: NSE stock/index derivatives and MCX commodities.
- Initial capital assumption: approximately INR 5-7 lakh; all actual limits are
  validated configuration, not universal constants.
- POC style: positional first, with one defined-risk stock/index options strategy.
- Later strategy plug-ins:
  - Positional stock/index option structures.
  - Directional premium-paid long stock/index calls/puts.
  - CAS/microstructure.
  - Positional commodity options/futures.
  - Directional commodity futures.
- Persistent core on a low-cost VPS; asynchronous weekly/post-market analytics may
  use serverless/batch compute.
- Broker/data candidates include OpenAlgo adapters and Fyers; implementations must
  be verified against current APIs before coding.

## Product objectives

1. No AI dependency for open-position protection.
2. Deterministic replay from exact data/config/code versions.
3. Broker reconciliation before accepting exposure after startup/reconnect.
4. Central portfolio-aware sizing, allocation, and risk control.
5. Independent strategy plug-ins using a common Trade Intent contract.
6. Complete audit lineage from market snapshot through close.
7. Bounded evaluator loops that propose, test, and promote changes safely.
8. Retail-grade cost while preserving safety and recoverability.

## Non-goals for the POC

- High-frequency or latency-arbitrage trading.
- AI-generated live orders, quantities, stops, or emergency decisions.
- Automatic LLM-driven parameter promotion.
- Naked short options.
- Many microservices, cross-cloud hot-path calls, or complex orchestration.
- Multiple brokers before one adapter/reconciliation path is proven.
- Building every strategy before one vertical slice is safe end to end.
- Treating backtest profitability as sufficient production evidence.

## Design principles

- Fail closed for new exposure; retain deterministic risk reduction.
- Broker is external truth; local durable events provide audit/recovery history.
- Pure calculations inside; side effects behind typed ports/adapters.
- Explicit time, units, freshness, state machines, and versions.
- Modular monolith first; split only for measured isolation/scaling/reliability.
- Configuration is versioned and validated; volatile market rules are not buried
  in code.
- Observability and remediation are system pillars, not an AI agent.

## Technology direction

- Python 3.11+ with strict type checking.
- Pydantic contracts and enums.
- Polars for tabular calculation; DuckDB/Parquet for POC research/replay.
- QuantLib where product conventions are explicitly validated.
- OpenAlgo or typed broker adapters for transport abstraction.
- Redis only when coordination/cache is justified; never position authority.
- Structured events/logs and a crash-safe durable event/state store for live path.

Technology is replaceable. Architectural authority boundaries are not.

