# Current State

LAST_UPDATED: 2026-09-13
CURRENT_MILESTONE: Phase 0 - Contracts and safety foundation (complete)
STATUS: PHASE_0_COMPLETE

## Confirmed decisions

- Four-layer architecture with a deterministic live path.
- Broker is external source of truth.
- Layer 2 exclusively owns live risk and execution.
- Layer 3 strategies emit Trade Intents.
- Layer 4 AI is asynchronous and proposal-only.
- POC starts with one positional defined-risk index options strategy.
- Persistent VPS core; optional serverless/batch analytics.
- Initial capital assumption approximately INR 5-7 lakh; limits remain config.
- `docs/context/` is canonical; `docs/research/` is superseded provenance.
- Python pinned to 3.11; `uv` for environment and lockfile.

## Implemented

Verified by running `uv run ruff format --check .`, `uv run ruff check .`,
`uv run mypy` and `uv run pytest`: 451 tests pass offline in about one second,
lint and `mypy --strict` clean across 30 files.

- **Agent guidance.** `.cursor/rules/` holds nine rules: `00-core` (always) plus
  `01-planning-context` and seven file-scoped specifications. `.cursorrules` was
  replaced; its previous contents transcribed the superseded design and
  instructed agents to violate five invariants.
- **Tooling.** `pyproject.toml` (ruff with DTZ/ANN/S/TID/PL, `mypy --strict` with
  `ignore-without-code`, pytest with `filterwarnings=error`),
  `.github/workflows/ci.yml`.
- **Dependency rule as a test.** `tests/test_import_boundaries.py` walks the AST
  of `src/trading/domain/` and fails on any import outside the standard library
  and pydantic, and fails if any production module calls `datetime.now`,
  `time.time`, `random` or `uuid4`.
- **Primitives.** `src/trading/domain/primitives.py`: `Money` (currency-tagged
  Decimal), `Price` (tick-grid enforced), `Quantity` and `Lots` (non-mixable),
  `Percent`, `LotSize`, `TickSize`. Float is rejected at every entry point.
  Rounding is named for the number line (`FLOOR`, `CEILING`, `TOWARD_ZERO`,
  `HALF_EVEN`); a property test proves `TOWARD_ZERO` never grows a magnitude.
- **Clock and IDs.** `domain/clock.py` (`Clock` protocol, `FrozenClock`,
  `SteppingClock`, naive datetimes rejected) and `domain/ids.py` (`IdFactory`,
  `SequentialIdFactory`, `derive_idempotency_key`). The key function takes no
  attempt number or timestamp, so a retry recomputes a byte-identical key.
- **State machines.** `domain/state/`: one generic guarded transition table plus
  System, Intent, Order and Trade machines. Illegal transitions raise
  `IllegalTransitionError` carrying the audit record.
  `tests/test_state_machines.py` walks the full state x state x trigger
  cross-product of all four machines.
- **Contracts.** `domain/contracts/`: `FeatureSnapshot`, `AIProposal`,
  `TradeIntent`, `RiskDecision`, `OrderEvent`, `ReconciliationEvent`, all frozen,
  `extra="forbid"`, versioned and losslessly round-trippable. A recursive
  payload check rejects floats anywhere, including nested inside a `Money` dict.
- **Configuration.** `config/schema.py`, `config/loader.py`, `config/base.yaml`.
  Every exchange-, regulator- and broker-controlled value ships null with the
  authority to check; reading one raises `ConfigNotVerifiedError` carrying
  `ReasonCode.CONFIG_UNVERIFIED`. A LIVE environment will not load while any
  market rule is unverified. The loader checksums file bytes for lineage.
- **Invariant index.** `tests/test_safety_invariants.py` covers invariants 2, 3,
  4, 6, 7, 9, 11, 12, 13, 14, 16, 17, 18, 19, 21, 23 and 25 with named tests. A
  meta-test parses `SAFETY_INVARIANTS.md` and fails if any of the 25 is neither
  covered nor explicitly deferred.

## In progress

- Nothing. Phase 0 is closed and no task is `READY`.

## Not yet confirmed

- Data and broker providers/adapters, and the transactional order-state store.
- Replay/backtest foundation and a point-in-time option-chain source.
- Portfolio, risk gateway, OMS and trade manager.
- Observability and deployment environment.
- AI provider and grounded evidence pipeline.
- POC instrument and strategy parameters.

## Active blockers

- Phase 1 cannot start until a point-in-time historical option-chain source is
  chosen. This is usually the longest lead-time item in the project.
- `config/base.yaml` contains no verified market rules, so any live-adjacent work
  begins with a verification pass against current official documentation.
- The eight invariants listed in `tests/test_safety_invariants.py::DEFERRED`
  remain unproven until the components they constrain exist.

## Next action

Resolve unresolved decision 4 in `docs/plans/ACTIVE_PLAN.md`: evaluate
point-in-time historical option-chain sources on history depth, point-in-time
guarantees and cost, then plan Phase 1 against the chosen source.

## Update rules

- Keep this file below 120 lines.
- Record facts with file/test evidence; do not paste logs or plans.
- Update only after a meaningful milestone or blocker change.
- The active plan owns task detail; this file owns current project truth.
