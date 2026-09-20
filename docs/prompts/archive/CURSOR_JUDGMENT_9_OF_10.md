# SUPERSEDED — do not paste into Cursor

Historical multi-phase paste pack (P0.1–P2.1). It overlaps core rules and is
**not** current Stage C/D work. Do not copy Phase blocks into Agent.

Current work uses `docs/plans/ACTIVE_PLAN.md`, `docs/plans/TASK_LEDGER.md`, and
`.cursor/rules/` (especially `00-core.mdc`). Kept for provenance only.

---

# Cursor prompt — Judgment 9/10 (token-efficient)

Copy **one Phase block at a time** into Cursor Agent. Do not paste the whole file.
Do not ask Cursor to “read the entire repo.”

---

## Global rules (prepend to every Phase)

```text
Repo: fno-automated (Indian Nifty F&O paper stack).
You are implementing a PLANNED slice only. Follow docs/plans/JUDGMENT_9_OF_10.md.

Hard constraints:
- Fail-closed. No LLM on live order path. config/agent.yaml stays enabled:false.
- Do not rewrite OMS/broker/Fyers adapters.
- Prefer extending existing modules over new packages.
- After edits: run only the touched tests + ruff/mypy on touched paths.
- Do not commit unless I ask.
- Keep diffs small. No drive-by refactors. No new deps unless unavoidable.
- Before editing: READ only the files listed in this phase (and their direct tests).

Definition of done for every phase:
1) Code + tests green for listed tests
2) 5–10 line summary of what changed and how to run it
```

---

## Phase A — P0.1 Fixture refuse (do this first)

```text
Goal: Weekly agent + scripts must not silently use fixture cohorts.

Read only:
- src/trading/cli.py (agent weekly path)
- src/trading/ai/loop.py
- scripts/oracle_agent_ask.sh
- tests/test_l4_agent.py
- docs/plans/JUDGMENT_9_OF_10.md section P0.1

Implement:
1) If no cohort path passed, do NOT default to tests/fixtures/**.
2) Add --allow-fixture flag (CLI + script) for explicit demo use.
3) When cohort missing and not allowed: exit non-zero with clear error:
   "refusing fixture/default cohort; pass a data/paper/cohorts/... JSON or --allow-fixture"
4) meta.json must include cohort_source: "paper"|"fixture"|"none"

Tests: extend tests/test_l4_agent.py for refuse + allow-fixture.
Run: pytest tests/test_l4_agent.py -q
Stop after this phase.
```

---

## Phase B — P0.3 SetupFeatures on routed intents

```text
Goal: Every routed paper intent carries SetupFeatures; eliminate empty feature counts when binders succeed.

Read only:
- src/trading/domain/contracts/identification.py
- src/trading/identification/binders.py
- src/trading/identification/router.py
- src/trading/runtime/paper_session.py
- src/trading/runtime/paper_runner.py
- tests/test_identification.py
- tests/test_paper_session.py (if exists)

Implement:
1) Ensure bind_long_option / bind_debit_spread always build SetupFeatures when eligible
   (already partially there — fill any None path on successful bind).
2) paper_session/paper_runner: copy setup_features onto TradeIntent / outcomes.
3) If route PASS, still record failed_gate_ids / rejected_alternatives on a diagnostic
   object persisted with the cycle (no fake features).

Tests: identification + paper_session asserting setup_features not None on emit.
Run: pytest tests/test_identification.py tests/test_paper_session.py -q
Stop after this phase.
```

---

## Phase C — P0.4 India VIX / IV in market_state

```text
Goal: build_market_state populates iv_percentile and iv_rv_ratio for Nifty sessions.

Read only:
- src/trading/identification/market_state.py
- src/trading/identification/config.py
- config/identification.yaml
- src/trading/data/backfill.py
- src/trading/data/fyers/client.py (history only)
- tests/test_identification.py

Implement:
1) Resolve India VIX Fyers symbol via instrument master (do not guess if lookup fails —
   add config key identification.vix_symbol with a verified default once found).
2) Allow history backfill for that symbol.
3) build_market_state: compute iv_percentile from VIX history window + iv_rv_ratio vs
   realized vol from Nifty bars already used for trend.
4) If VIX missing: set explicit MacroStatus/gap reason; do not invent numbers.

Tests: unit test with synthetic VIX series; one test for missing VIX fail-visible.
Run: pytest tests/test_identification.py -q
Stop after this phase.
```

---

## Phase D — P1.1 + P1.2 Structure advise CLI

```text
Goal: trading agent advise returns StructureAdvice JSON using DeepSeek tools.

Read only:
- docs/plans/JUDGMENT_9_OF_10.md section 7 (schema)
- src/trading/ai/loop.py, tools.py, openai_compat.py, ports.py, budget.py
- src/trading/cli.py agent subcommands
- config/agent.yaml
- tests/test_l4_agent.py, tests/test_l4_openai_compat.py

Implement:
1) Domain model StructureAdvice (Pydantic) matching plan schema; validate preferred_structure enum.
2) New tool loop OR reuse weekly tools with a tighter system prompt: rank families only,
   allow PASS, forbid ENABLE/live language.
3) CLI: `trading agent advise [--symbol ...] [--allow-fixture]` writes under
   data/paper/agent_runs/.../advice.json and prints summary.
4) Server-side reject unknown structures.
5) Max iterations ≤ 6; reuse TokenBudget.

Tests: schema validation; mock LLM returning advice; invalid structure rejected.
Run: pytest tests/test_l4_agent.py tests/test_l4_openai_compat.py -q
Also add scripts/oracle_agent_advise.sh mirroring oracle_agent_ask.sh but calling advise.
Stop after this phase.
```

---

## Phase E — P1.3 Router allow-table

```text
Goal: Deterministic family allowlist from regime before LLM advise.

Read only:
- src/trading/identification/router.py
- config/identification.yaml
- tests/test_identification.py

Implement:
1) YAML table: trend × vol_regime × iv_bucket × event × session → allowed_families.
2) route_nifty_options intersects binder eligibility with allow table.
3) Credit/multileg only if iv_bucket high and trend range-like; CAS only in auction window;
   commodity not selected from Nifty route (separate entrypoint ok).

Tests: matrix of 6–8 regime cases.
Run: pytest tests/test_identification.py -q
Stop after this phase.
```

---

## Phase F — P2.1 Judgment harness (after paper data exists)

```text
Goal: Offline judgment metrics from cohorts.

Read only:
- src/trading/analytics/scorecard.py
- src/trading/analytics/eligibility.py
- src/trading/cli.py evaluate*
- tests/test_l4_scorecard.py

Implement:
1) Label trades should_enter/should_pass using MAE/MFE − charges_per_lot.
2) CLI trading evaluate judgment --cohort PATH → precision, capture, brier (if conf present).
3) Document thresholds in config/evaluation.yaml under judgment: (new section).

Tests: fixture cohort with known labels.
Run: pytest tests/test_l4_scorecard.py -q
Stop after this phase.
```

---

## How to use in Cursor (token tips)

1. New Agent chat per Phase (A→F). Paste Global rules + one Phase only.
2. If context grows: “continue Phase X; do not re-read finished files.”
3. After each phase: commit yourself with a focused message.
4. Ops-only P0.2 (Monday paper session) — do not ask Cursor to OAuth; human step.
5. Prefer Composer/Agent on listed files only; avoid @Codebase unless stuck.

## One-shot mega-prompt (only if you accept larger context)

```text
Implement Phases A–E from docs/prompts/CURSOR_JUDGMENT_9_OF_10.md in order.
Stop at the first failing test; do not skip ahead. Follow Global rules there.
```
