# Four-Layer Architecture

## Control flow

```text
External data -> Layer 1 FeatureSnapshot -> Layer 3 TradeIntent
              -> Layer 2 RiskDecision/OMS -> Broker

Broker events -> Layer 2 reconciliation/portfolio/trade management
Audit evidence -> Layer 4 forward validation -> eligibility / proposal -> promotion gate
```

No arrow goes directly from AI or a strategy to the broker. Layers 1–3 must remain live-safe if every Layer 4 job is down.

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

## Layer 4: Forward Validation, Evaluation and Controlled Promotion

Layer 4 answers whether a *frozen* strategy version has shown reliable
live-forward behaviour under conservative fills, enough observations and zero
safety violations — and whether it is eligible for a tightly limited canary.
It does not answer whether a historical backtest Sharpe looked good.

It also runs an **asynchronous weekly agent loop** that may propose which
already-coded strategy families to enable, shadow or halt for a period. That
loop never sizes, stops or submits. Intraday decisions stay Layer 3 + Layer 2.

Nested cadence:

| Horizon | Who decides | LLM? |
| --- | --- | --- |
| Intraday | L3 intents + L2 risk/OMS | Never |
| Pre-open day | Deterministic readiness + last unexpired weekly proposal | No, unless a blocker needs the operator |
| Week / period | Bounded L4 tool loop over scorecards and evidence | Yes, capped; fail closed to `ABSTAIN` |

Agent Desk authority mode **BOUNDED** (decision C1, 2026-09-20) means
**config-promotion only**: a signed grant may let an agent propose enabling,
shadowing or halting already-coded strategy families through the L4 proposal
path. It never authorises intraday sizing, stops, submits, or any other live-path
action. Intraday remains L3 + L2 with no LLM.

Asynchronous, advisory components (local `trading.analytics` and `trading.ai`):

- Conservative fill reconstruction from decision-time bid/ask/depth.
- Deterministic post-trade / EOD scorecards over one experiment + fill-model
  version. Versions are never pooled.
- Fail-closed promotion eligibility (`ELIGIBLE` / `INELIGIBLE` /
  `INSUFFICIENT_SAMPLE`). Win rate or gross P&L alone never pass.
- Operator `AttentionRequest` for charges, CAS features and unverified LIVE
  config. Telegram/CLI only; no live levers.
- Weekly Anthropic-style tool loop (`read_scorecard`, `read_eligibility`,
  `query_cohort`, `fetch_market`, `fetch_news_snapshot`,
  `request_operator_attention`, `emit_proposal`) emitting an expiring
  `STRATEGY_FAMILY` `AIProposal`. Token/iteration caps live in `config/agent.yaml`
  (`enabled: false` until paper evidence exists). Deterministic scorecards remain
  authoritative P&L.

Output is an evidence package. It cannot mutate live configuration, raise
account risk, enable naked shorts, touch the kill switch, swap credentials or
convert `PAPER`→`LIVE`. Promotion:

```text
SHADOW -> PAPER -> CANARY_REAL -> LIMITED_REAL -> NORMAL_REAL
                                  \-> SUSPENDED

scorecard -> eligibility (ELIGIBLE / INELIGIBLE / INSUFFICIENT_SAMPLE)
         -> human + deterministic gate
         -> signed config (never written by Layer 4) -> deployment
```

Full historical backtest / walk-forward is out of the promotion path. Recorded
session replay stays for incident reconstruction only.

## Cross-cutting pillars

- Observability: metrics, structured logs, traces, heartbeat and audit.
- Remediation: bounded retries, circuit breakers, recovery state and runbooks.
- Security: least privilege, separated environments, secret management.
- Governance: versioned contracts/config, promotion, rollback and lineage.

## Runtime placement

Oracle VM (must trade safely if every Layer 4 job is down):

- Layer 1 feeds and features, Layer 2 risk/OMS/exits/kill switch, Layer 3
  strategies, paper fill routing, approved config, local health.

Asynchronous jobs (local `trading.analytics` / `trading.ai`; OCI Functions later):

- Scorecard, eligibility, operator attention scan, weekly `STRATEGY_FAMILY` loop.
- External VM-heartbeat watchdog (later ops slice; must not place orders).

If Layer 4 is down: existing positions keep Layer 2 exits; no new parameter
versions; trading halts only if a strategy **requires** fresh macro context.

The persistent core cannot require a cross-cloud or LLM call to remain live-safe.

## Dependency rules

- Domain and calculations import no infrastructure.
- Adapters implement application/domain ports.
- Strategies depend on domain contracts, not OMS/broker adapters.
- Analytics may read durable evidence but cannot write live state.
- Dashboard/read models are derived and disposable.

