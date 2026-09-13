# Trading Platform

Autonomous trading platform for Indian markets. Deterministic software owns
every live decision; AI improves research, context and evaluation outside the
live path. This is an automated trading system with AI support, not an AI
trading system.

## Layout

| Path | Contents |
| --- | --- |
| `docs/context/` | Canonical architecture, safety and specification set |
| `docs/plans/` | Active implementation plan and task ledger |
| `docs/research/` | Superseded source documents, kept for provenance only |
| `.cursor/rules/` | Agent rules: core plus file-scoped specifications |
| `src/trading/` | Implementation |
| `config/` | Versioned, validated configuration |
| `tests/` | Unit, invariant, contract and scenario tests |

`docs/research/` describes an earlier design that the canonical context
replaces. Never implement from it.

## Development

    uv sync
    uv run ruff check .
    uv run mypy src
    uv run pytest

## Planning a milestone

Start Cursor Agent in planning mode and apply `@planning-context`. It reads the
canonical context once, inspects the repository, and writes the approved digest
to `docs/plans/ACTIVE_PLAN.md` before any coding.

## Implementing

    Implement the next READY item from docs/plans/TASK_LEDGER.md using
    docs/plans/ACTIVE_PLAN.md. Do not reread the full planning context. Patch
    minimally, run the listed tests, then update the ledger and current state.

During implementation the agent reads `.cursor/rules/00-core.mdc`
automatically, plus `ACTIVE_PLAN.md`, `CURRENT_STATE.md`, the rules matching
the files being changed, and narrow source ranges found with `rg`. It must not
reread the full context pack unless the task changes architecture, the active
plan declares `CONTEXT_REFRESH_REQUIRED: yes`, or a contradiction cannot be
resolved from the plan and the relevant scoped specification.

## Maintenance

- Update `docs/context/CURRENT_STATE.md` after meaningful milestones.
- Replace `docs/plans/ACTIVE_PLAN.md` when beginning a new milestone.
- Keep `docs/plans/TASK_LEDGER.md` factual and compact.
- One strategy specification per strategy, from the supplied template.
- Volatile broker and exchange values belong in validated configuration, never
  in prose or source constants.
