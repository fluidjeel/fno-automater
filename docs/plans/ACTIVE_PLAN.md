# Active Implementation Plan

PLAN_STATUS: APPROVED
CONTEXT_DIGEST_VERSION: 8
PLANNED_AT: 2026-09-19
CONTEXT_REFRESH_REQUIRED: no

Milestone: **Phase 4–6 — unattended PAPER session**.

## Goal

Keep Layers 1–3 as the live trading core. Run them unattended in PAPER against
live L1 snapshots, persist frozen cohorts, and keep Layer 4 proposal-only. AI
never places an order or writes config. Historical backtesting remains waived.

## Nested cadence (do not collapse these)

1. **Intraday** — `trading paper session`: deterministic L3 `TradeIntent` + L2
   risk/OMS on the paper broker. No LLM.
2. **Pre-open day** — Telegram Fyers OAuth, then the session loop. No LLM unless
   an `AttentionRequest` blocks.
3. **Week / period** — `trading agent weekly` may emit an expiring
   `STRATEGY_FAMILY` `AIProposal`. Disabled by default in `config/agent.yaml`.

Champion–challenger is parallel SHADOW/PAPER under existing `risk.yaml`
fractions, not a daily winner lottery.

## Current foundation

- `config/paper.yaml` is `Environment.PAPER` / `ACC-PAPER-1`. `base.yaml` stays
  BACKTEST.
- `trading paper session` authenticates via Telegram, polls L1, builds
  master-backed candidates, scores news event-risk, runs all five strategies
  through `PaperRunner`, manages exits, notifies on Telegram, and writes
  `data/paper/cohorts/` at ~15:40 IST.
- PAPER isolation still refuses Fyers transaction adapters.
- Conservative `fill_model` is on for the session; L2 E2E remains immediate-fill.

## Remaining work

1. PAPER-005 — human promotion record after eligibility, drills and verified
   charges. The agent may summarise; it cannot sign.
2. Live close-window CAS cohort now that PAPER stance is on and Monday depth
   can complete `cas-microstructure-v1`.
3. Production `LlmPort` is DeepSeek OpenAI-compat (`OpenAICompatLlm`); keep `config/agent.yaml` enabled:false until paper evidence justifies spend. Trial: `trading agent weekly --trial`.

## Blocking gaps

- `evaluation.yaml` `charges_per_lot.verified_at` is null, so net expectancy
  stays `None` and promotion stays `INELIGIBLE`.
- LIVE `config/base.yaml` market-rule values remain unverified.
- No production LLM client.

## Acceptance

- PAPER cannot submit through a live broker.
- Missing event-risk or stale snapshots block new entries.
- Restart reuses idempotency keys; Telegram copy is advisory.
- Promotion remains a signed human config change.

## Non-goals

- LLM in the live decision path.
- Daily strategy lottery, auto-promotion, or Fyers order/span APIs on this
  process.
