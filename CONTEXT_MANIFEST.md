# Context Manifest

Rules live in `.cursor/rules/`. Canonical context lives in `docs/context/`.
Implementation memory lives in `docs/plans/`.

## Automatically applied

- `.cursor/rules/00-core.mdc`: compact safety and low-token execution rules.

## Planning only

- `.cursor/rules/01-planning-context.mdc`: reads the canonical context once and
  produces `docs/plans/ACTIVE_PLAN.md` plus `TASK_LEDGER.md`.

## File-scoped rules

- `10-domain-contracts.mdc`: domain models and contract tests.
- `20-data-calculations.mdc`: feeds, data, calculations and replay.
- `30-risk-execution.mdc`: portfolio, risk, OMS, broker and recovery.
- `40-strategies.mdc`: strategy plug-ins and specifications.
- `50-analytics-ai.mdc`: asynchronous analytics and AI.
- `60-testing-release.mdc`: tests, backtests, CI and release.
- `70-observability-operations.mdc`: telemetry, deployment and operations.

## Canonical planning context

- `PROJECT_CONTEXT.md`: mission, scope, non-goals and technology direction.
- `ARCHITECTURE.md`: layers, flows, ownership and runtime placement.
- `SAFETY_INVARIANTS.md`: non-negotiable live-safety properties.
- `DOMAIN_CONTRACTS.md`: typed messages and state machines.
- `DATA_SPEC.md`: data, quality, calculation and replay requirements.
- `BROKER_SPEC.md`: adapter, order and reconciliation behavior.
- `TESTING_AND_RELEASE.md`: evidence, tests and promotion gates.
- `OPERATIONS_RUNBOOK.md`: readiness, failure and remediation procedures.
- `POC_ROADMAP.md`: safe vertical build order.
- `CURRENT_STATE.md`: concise evidence-backed project status.
- `PAPER_DATA_REQUIREMENTS.md`: P0/P1 paper market-data matrix and gates.
- `STRATEGY_SPECS/`: relevant strategy specification only.

## Superseded

`docs/research/` holds the three source documents this context pack replaces:
the layered architecture DOCX, the autonomous-system research PDF and the
originating chat transcript. They are provenance, not specification. The
Service/Agent topology, AI-in-the-loop execution, CAS lottery trade, Redis as
position authority and hard-coded exchange thresholds found there are rejected.

## Implementation memory

- `docs/plans/ACTIVE_PLAN.md`: compact approved digest used after planning.
- `docs/plans/TASK_LEDGER.md`: next task and completed verification evidence.

The canonical context is read during planning. Implementation must not reload it
unless `CONTEXT_REFRESH_REQUIRED: yes` or a material contradiction/architecture
change makes the digest invalid.
