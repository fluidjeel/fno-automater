# Judgment Quality → 9/10 (Process, Trade-pick, Promotion Gate)

Status: PLAN  
Created: 2026-09-19  
Repo: `fno-automated`  
Target scores (from 2026-09-19 audit): process 8 → 9, trade-pick 4 → 9, promotion gate 7.5 → 9

## 1. Problem statement

The Layer-4 weekly agent is a **promotion governor** (ENABLE / SHADOW / HALT / REVISE).
It is **not** a structure desk. Treating one DeepSeek weekly call as “what to trade”
caps trade-pick quality. Fixture cohorts and null IV make even the governor half-blind.

## 2. Score definitions (acceptance)

### 2.1 Process / risk = 9
- Live/ask paths never load `tests/fixtures/**` cohorts unless `--allow-fixture`.
- ENABLE forbidden unless: real paper cohort, `costs_confirmed`, sample gates pass,
  no P0/P1, setup features present.
- Every ABSTAIN/SHADOW/HALT cites machine-readable `failed_gate_ids[]`.
- Shipped `config/agent.yaml` stays `enabled: false`; `--enable` is override-only.

### 2.2 Trade-pick = 9
- Separate `trading agent advise` command ranks **all** paper families:
  `positional_long_option`, `debit_spread`, `defined_risk_multileg`,
  `cas_microstructure`, `commodity_futures_trend`.
- Output is structured JSON (schema below), not free prose only.
- Ranking uses market state (trend + vol + IV/VIX + event + session window) and
  binder eligibility; missing IV on index sessions is a hard gap, not silent None.
- Includes `do_not_trade_if[]` and explicit pass option.

### 2.3 Promotion gate = 9
- Offline judgment harness: labels `should_enter|should_pass` from ex-post path
  minus verified charges; track precision, capture, Brier on confidence.
- ENABLE only when gate metrics for that policy version clear thresholds.
- Confidence is calibrated or ENABLE is refused.

## 3. Non-goals
- No LLM on live order path.
- No auto-ENABLE to live / canary.
- No rewriting L2 OMS or broker adapters.
- No model upgrade as the primary lever (DeepSeek is fine).

## 4. Workstreams & slices

### P0 — Honest evidence (process → 9, gate → ~8.5)

| ID | Slice | Deliverable | Done when |
| --- | --- | --- | --- |
| P0.1 | Fixture refuse | Weekly + ask scripts reject fixture/default cohort without `--allow-fixture` | Test + CLI error message |
| P0.2 | Real paper cohort | Document Monday OAuth → `paper session` → cohort path; agent reads only `data/paper/cohorts/**` | One real cohort JSON on disk after a session |
| P0.3 | SetupFeatures always | Binders/router always attach `SetupFeatures` on routed intents; paper runner persists them | Scorecard `setup_feature_count > 0`; CAS_FEATURES_MISSING clears when data present |
| P0.4 | IV / India VIX | Backfill `NSE:INDIAVIX-INDEX` (confirm symbol in instrument master); `build_market_state` fills `iv_percentile` / `iv_rv_ratio` | Router no longer returns null preferred solely for missing IV on Nifty sessions |

### P1 — Structure desk (trade-pick → 9)

| ID | Slice | Deliverable | Done when |
| --- | --- | --- | --- |
| P1.1 | Advise schema | `StructureAdvice` contract + JSON schema | Round-trip tests |
| P1.2 | `trading agent advise` | CLI using DeepSeek tools: snapshot, IV, events, binder eligibility, last scorecards | One command prints ranked structures |
| P1.3 | Router table | Expand `route_*` / policy YAML: trend×vol×iv×event×session → allowed families | Unit matrix tests |
| P1.4 | Pass discipline | Max signals/day, min score gap, cooldown already partially present — wire into advise + paper | Fire rate drops in paper metrics |

### P2 — Prove the judge (gate → 9)

| ID | Slice | Deliverable | Done when |
| --- | --- | --- | --- |
| P2.1 | Judgment harness | Offline labels + metrics report CLI | `trading evaluate judgment` |
| P2.2 | Calibration | Confidence buckets vs ENABLE success | Uncalibrated → no ENABLE |
| P2.3 | Counterfactual shadow | Same window, all families shadowed; advise cites features | Narrative references feature diffs |

## 5. Suggested calendar

- **Days 1–2:** P0.1, P0.3 (code), P0.4 (data+code)
- **Days 3–5:** P1.1, P1.2
- **Week 2:** P0.2 (ops Monday), P1.3, P1.4
- **Week 3–4:** P2.1 then P2.2

## 6. Key files (read before edit)

- `src/trading/ai/loop.py` — `run_weekly_agent`
- `src/trading/ai/tools.py`, `openai_compat.py`, `ports.py`
- `src/trading/identification/{market_state,router,binders,config}.py`
- `src/trading/domain/contracts/identification.py` — `SetupFeatures`
- `src/trading/runtime/paper_session.py`, `paper_runner.py`
- `config/{agent,identification,evaluation,paper_session}.yaml`
- `scripts/oracle_agent_ask.sh`
- `tests/test_identification.py`, `tests/test_l4_agent.py`

## 7. StructureAdvice schema (target)

```json
{
  "as_of": "ISO-8601",
  "preferred_structure": "debit_spread|positional_long_option|defined_risk_multileg|cas_microstructure|commodity_futures_trend|PASS",
  "stance": "PAPER|SHADOW|SUSPENDED|PASS",
  "confidence": 0.0,
  "alternatives_ranked": [{"structure": "...", "score": 0.0, "why": "..."}],
  "do_not_trade_if": ["..."],
  "invalidation": ["..."],
  "evidence_ids": ["..."],
  "failed_gate_ids": [],
  "market_summary": {"trend": "...", "iv_percentile": null, "iv_rv_ratio": null}
}
```

## 8. Risks
- VIX symbol / history entitlement on Fyers — confirm with instrument master before hard-coding.
- Advise LLM inventing structures outside allowlist — validate enum server-side.
- Token cost — advise tools return truncated JSON; max iterations low (≤6).
