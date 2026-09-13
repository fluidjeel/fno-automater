# Active Implementation Plan

PLAN_STATUS: APPROVED
CONTEXT_DIGEST_VERSION: 3
PLANNED_AT: 2026-09-13
CONTEXT_REFRESH_REQUIRED: no

Milestone: **Phase 1 - Live Layer 1 completeness (Fyers)**.

## Goal

Close the live Fyers Layer 1 gap: one `trading data fetch` plus optional WS daemon
produces a quality-gated FeatureSnapshot with quotes, bars, chain, OI, depth,
market status, expiry/reference and vendor Greeks. JSONL remains replay
authority; Parquet/DuckDB is a derived catalog.

## Scope

- REST: depth, marketStatus, chain `greeks=1`, history `oi_flag`, optional expiry.
- Quality: session, warmup, clock drift, cross-source, spread/depth, market status.
- Snapshot features as Decimals (ATM Greeks, OI/PCR, VIX, depth qty). INDEX
  snapshots do not carry DerivativesContext.
- Catalog writer; WS `--daemon` + systemd unit.

## Non-goals

- Historical expired F&O / vendor option-chain backfill.
- QuantLib IV solver, indicators, OFI, Layer 2 broker/OMS, news changes.

## Context digest

- Domain FeatureSnapshot for INDEX cannot attach DerivativesContext.
- Session hours live in config; unverified session fails closed.
- Fyers Greeks are vendor BS (`greeks_calculation_version: fyers_chain`).
- Options-structure replay remains blocked without a historical chain source.

## Vertical slices

| Slice | Outcome | Acceptance |
| --- | --- | --- |
| L1-001 Feeds | depth/status/greeks/OI normalize + pipeline | Offline fixtures; empty chain INVALID; empty depth DEGRADED |
| L1-002 Quality | session/warmup/drift/cross-source | Invariant-6 tests |
| L1-003 Snapshot | OI/PCR/Greeks/depth sizes in features | Missing Greeks omit keys |
| L1-004 Catalog | Parquet + DuckDB after fetch | Event id count matches JSONL |
| L1-005 WS daemon | `--daemon` + data-tick.service | Offline reconnect test |

## Residual risks

- NSE hours in config must stay aligned with official documentation.
- expiry endpoint may 404; treated as optional.
- Phase 1 options backtest still needs a third-party historical chain.
