# Active Implementation Plan

PLAN_STATUS: APPROVED
CONTEXT_DIGEST_VERSION: 1
PLANNED_AT: 2026-09-13
CONTEXT_REFRESH_REQUIRED: no

Milestone: **Phase 0 - Contracts and Safety Foundation**. Delivered. This file is
the compact authoritative digest; implementation turns do not reread the full
context pack.

## Goal

Make the four-layer context pack executable rather than aspirational: the types,
state machines, configuration and tests that later phases must not be able to
violate by accident.

## Scope

Repository hygiene and agent rules; tooling and CI; decimal primitives with
injected clock and IDs; four guarded state machines; six versioned contracts;
validated configuration; a named invariant suite.

## Non-goals

No feed adapter, broker adapter, OMS, portfolio, sizing, strategy, pricing model,
Redis, DuckDB or AI. No CAS logic, no Kelly implementation. Nothing in this
milestone touches a network; the whole suite runs offline in about a second.

## Context digest

- `docs/context/` is canonical. `docs/research/` holds three superseded design
  documents and is provenance only. The Service/Agent topology, AI-in-the-loop
  execution, CAS lottery trade, Redis as position authority and hard-coded SEBI
  thresholds found there are rejected designs.
- Layer ownership: 1 Data owns feeds, quality and features; 2 Risk owns
  portfolio, sizing, limits, OMS, exits and reconciliation; 3 Strategy owns setup
  logic and requested risk; 4 Analytics is asynchronous and proposal-only.
- Dependency direction: the domain layer imports only the standard library and
  pydantic. Enforced by `tests/test_import_boundaries.py`, not by convention.
- Authority is encoded in types. `TradeIntent` has no field that can express a
  quantity or a broker command, and `AIProposal` has no field that can promote
  itself. Tests assert those absences.
- Volatile market rules live in `config/base.yaml` as `VerifiedValue` entries and
  fail closed until verified. Never a literal in domain logic.
- Python is pinned to **3.11**, not the 3.12 originally proposed: 3.11.15 was
  already installed, satisfies the `>=3.11` requirement, and has the widest
  QuantLib and Polars wheel coverage, which was the reason for pinning away from
  the host's 3.14 in the first place.

## Vertical slices

All seven are complete. Evidence is in `docs/context/CURRENT_STATE.md` and in the
commit for each slice.

| Slice | Outcome | Key files | Acceptance |
| --- | --- | --- | --- |
| 0 Hygiene | `docs/` canonical, nine agent rules exist, `.cursorrules` no longer contradicts the invariants | `.cursor/rules/*.mdc`, `.cursorrules` | No duplicated markdown; every `CONTEXT_MANIFEST.md` path resolves |
| 1 Tooling | Offline, typed, linted CI with an executable dependency rule | `pyproject.toml`, `.github/workflows/ci.yml`, `tests/test_import_boundaries.py` | ruff, mypy strict and pytest green |
| 2 Primitives | Units in types; no float in accounting; injected time and IDs | `domain/primitives.py`, `domain/clock.py`, `domain/ids.py` | Property tests on rounding; byte-identical idempotency key across processes |
| 3 State | Four guarded machines; illegal transitions raise and audit | `domain/state/` | Full state x state x trigger cross-product classified |
| 4 Contracts | Six strict versioned models, lossless round-trip | `domain/contracts/` | Fuzz suite yields validation errors, never partial objects |
| 5 Config | Every market rule validated, checksummed, fails closed | `config/schema.py`, `config/loader.py`, `config/base.yaml` | Unverified value raises; checksum stable and change-sensitive |
| 6 Invariants | Named tests indexed to `SAFETY_INVARIANTS.md` | `tests/test_safety_invariants.py` | Meta-test proves all 25 are covered or explicitly deferred |

## Unresolved decisions

These block later phases, not this one. Each needs evidence before code.

1. **Broker and data provider.** OpenAlgo abstraction versus a direct typed Fyers
   adapter. Needs current API verification for idempotency support, client order
   IDs, partial-fill semantics and rate limits. Blocks Phase 2.
2. **Transactional store for order state.** SQLite in WAL mode with a single
   writer versus PostgreSQL from the first release. Blocks Phase 2.
3. **POC underlying and defined-risk structure.**
   `docs/context/STRATEGY_SPECS/01_POSITIONAL_INDEX_OPTIONS_POC.md` is still
   `STATUS: RESEARCH` with no parameters set. Blocks Phase 4.
4. **Point-in-time historical option-chain source** for replay. Hard dependency
   for Phase 1 and typically the longest lead-time item.
5. **Retention periods** for raw ticks, decision episodes, audit logs and AI
   evidence. Placeholders exist in `config/base.yaml` under `storage`.

## Residual risks

- The eight deferred safety invariants listed in
  `tests/test_safety_invariants.py::DEFERRED` are unproven until the components
  they constrain exist. The meta-test prevents them being quietly forgotten.
- `config/base.yaml` ships with every market rule null. That is intentional, but
  it means Phase 1 begins with a verification task, not with coding.
- Contract schemas are at version 1 with no consumers yet, so the first
  compatibility migration is untested.

## Implementation handoff

Next milestone is Phase 1 per `docs/context/POC_ROADMAP.md`: historical data
foundation and replay. Start by resolving unresolved decision 4, since a
point-in-time option-chain source gates everything in that phase.
