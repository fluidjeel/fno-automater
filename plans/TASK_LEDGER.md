# Task Ledger

ACTIVE_PLAN_VERSION: 0

Use statuses `READY`, `IN_PROGRESS`, `BLOCKED`, `DONE`. Exactly one task may be
`READY` or `IN_PROGRESS`. Keep completed evidence concise.

| ID | Status | Outcome | Scope | Verification | Dependency |
| --- | --- | --- | --- | --- | --- |
| PLAN-001 | READY | Create initial evidence-based plan | Context and repository inspection | ACTIVE_PLAN approved | None |

## Completion record format

```text
ID:
Result:
Changed files:
Tests:
Residual risk:
Next READY item:
```

Do not paste logs, diffs, architecture prose or repeated context into this file.

