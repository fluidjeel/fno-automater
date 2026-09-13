# Current State

LAST_UPDATED: not-started
CURRENT_MILESTONE: Phase 0 - Repository and contracts
STATUS: PLANNING_REQUIRED

## Confirmed decisions

- Four-layer architecture with deterministic live path.
- Broker is external source of truth.
- Layer 2 exclusively owns live risk and execution.
- Layer 3 strategies emit Trade Intents.
- Layer 4 AI is asynchronous and proposal-only.
- POC starts with one positional defined-risk index options strategy.
- Persistent VPS core; optional serverless/batch analytics.
- Initial capital assumption approximately INR 5-7 lakh; limits remain config.

## Implemented

- None confirmed. Cursor must inspect the repository during initial planning and
  replace this section with evidence-backed status.

## In progress

- Initial repository assessment and active implementation plan.

## Not yet confirmed

- Package structure and Python tooling.
- Data and broker providers/adapters.
- Domain contracts and durable state store.
- Replay/backtest foundation.
- Portfolio/risk/OMS/trade manager.
- Observability and deployment environment.
- AI provider and grounded evidence pipeline.

## Active blockers

- Actual repository state has not been inspected.
- Final POC instrument/universe and strategy parameters require specification.
- Current broker/data API capabilities must be verified before adapter coding.

## Next action

Invoke `@planning-context`, inspect the repository, write `ACTIVE_PLAN.md`, and
populate `TASK_LEDGER.md`. Do not implement broad scaffolding before that plan.

## Update rules

- Keep this file below 120 lines.
- Record facts with file/test evidence; do not paste logs or plans.
- Update only after a meaningful milestone or blocker change.
- The active plan owns task detail; this file owns current project truth.

