# Task Ledger

ACTIVE_PLAN_VERSION: 1

Use statuses `READY`, `IN_PROGRESS`, `BLOCKED`, `DONE`. Exactly one task may be
`READY` or `IN_PROGRESS`. Keep completed evidence concise.

| ID | Status | Outcome | Scope | Verification | Dependency |
| --- | --- | --- | --- | --- | --- |
| P0-000 | DONE | Repo hygiene and doc canonicalization | `docs/`, `.cursor/rules/`, `.cursorrules` | Manifest paths resolve; no duplicate markdown | None |
| P0-001 | DONE | Tooling, CI, executable dependency rule | `pyproject.toml`, `.github/workflows/ci.yml` | `tests/test_import_boundaries.py` | P0-000 |
| P0-002 | DONE | Primitives, clock, idempotency key | `src/trading/domain/{primitives,clock,ids}.py` | `tests/test_primitives.py`, `tests/test_clock_and_ids.py` | P0-001 |
| P0-003 | DONE | Enums and four guarded state machines | `src/trading/domain/{enums.py,state/}` | `tests/test_state_machines.py` | P0-002 |
| P0-004 | DONE | Six versioned domain contracts | `src/trading/domain/contracts/` | `tests/test_contracts.py` | P0-003 |
| P0-005 | DONE | Configuration schema, loader, base config | `src/trading/config/`, `config/base.yaml` | `tests/test_config.py` | P0-004 |
| P0-006 | DONE | Named invariant suite and handoff | `tests/test_safety_invariants.py`, `docs/` | Meta-test classifies all 25 invariants | P0-005 |
| P1-000 | BLOCKED | Select a point-in-time historical option-chain source | Research and vendor evaluation | Written comparison with cost, history depth and point-in-time guarantees | Unresolved decision 4 in ACTIVE_PLAN |

## Completion record format

```text
ID:
Result:
Changed files:
Tests:
Residual risk:
Next READY item:
```

## Latest completion record

```text
ID: P0-006
Result: Phase 0 complete. 451 tests pass offline in about one second; ruff format,
  ruff check and mypy --strict are clean on 30 files.
Changed files: tests/test_safety_invariants.py; docs/plans/ACTIVE_PLAN.md;
  docs/plans/TASK_LEDGER.md; docs/context/CURRENT_STATE.md
Tests: 17 invariants covered by named tests; 8 explicitly deferred with the phase
  that owns them; a meta-test fails if any of the 25 is left unclassified.
Residual risk: the 8 deferred invariants are unproven until Phases 1-6 build the
  components they constrain. Contract schema version 1 has no consumers, so the
  first compatibility migration is untested.
Next READY item: P1-000 is BLOCKED pending unresolved decision 4. No task is
  READY; the next step is a research decision, not code.
```

Do not paste logs, diffs, architecture prose or repeated context into this file.
