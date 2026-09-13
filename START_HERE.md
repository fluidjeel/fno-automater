# Trading Platform Context Pack

Extract this archive at the repository root. Existing source files are not
included and should not be overwritten.

## First planning session

Start Cursor Agent in planning mode and send:

```text
Plan the next implementation milestone. Apply @planning-context. Read the full
canonical context exactly once, inspect the current repository, and write the
approved implementation digest to docs/plans/ACTIVE_PLAN.md before coding.
```

During planning, Cursor reads the canonical context set. During implementation,
it reads only:

1. `.cursor/rules/00-core.mdc` automatically.
2. `docs/plans/ACTIVE_PLAN.md` and `docs/context/CURRENT_STATE.md`.
3. Rules and specifications matching the files being changed.
4. Narrow source/test ranges discovered with `rg`.

It must not reread the full context pack unless the task changes architecture,
the active plan declares `CONTEXT_REFRESH_REQUIRED: yes`, or a contradiction
cannot be resolved from the plan and relevant scoped specification.

## Normal implementation prompt

```text
Implement the next READY item from docs/plans/TASK_LEDGER.md using
docs/plans/ACTIVE_PLAN.md. Do not reread the full planning context. Patch
minimally, run the listed tests, then update the ledger and current state.
```

## Files intended for maintenance

- Update `CURRENT_STATE.md` after meaningful milestones.
- Replace `ACTIVE_PLAN.md` when beginning a new milestone.
- Keep `TASK_LEDGER.md` factual and compact.
- Create one strategy specification per strategy from the supplied template.
- Put volatile broker/exchange values in validated configuration, never prose or
  source constants.

