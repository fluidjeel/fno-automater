# Current State

LAST_UPDATED: 2026-09-13
CURRENT_MILESTONE: Phase 1 - Market data and advisory news evidence
STATUS: PHASE_1_DATA_FOUNDATION_IN_PROGRESS

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

## Implemented since Phase 0

- **Market-data foundation (live Fyers):** REST quotes, OHLCV (+OI flag), option
  chain with vendor Greeks, 5-level depth, market status, expiry/reference,
  JSONL authority plus Parquet/DuckDB catalog, quality gates (session, warmup,
  drift, cross-source), snapshot features, replay, and WS ticks
  (`trading data stream --daemon`). See `src/trading/data/` and
  `config/data_pipeline.yaml`.
- **News/macro evidence subsystem:** strict records and taxonomy; GDELT and
  configured official RSS collectors; key-gated FRED and route-disabled EIA adapters; bounded
  retry/circuit behavior; canonical URL and content hashes; event clustering;
  explicit unavailable sentiment; optional local-only FinBERT; deterministic
  weighted asset impacts and advisory event-risk snapshots; idempotent JSONL
  persistence; CLI and an abstaining proposal default. See
  `docs/context/NEWS_SUBSYSTEM.md` and `src/trading/news/`.
- **Verification:** 495 tests pass offline, including source fixtures,
  deterministic scoring, syndication deduplication, UNKNOWN sentiment, strict
  contracts and storage idempotency. News does not affect the broker or open
  positions. Source weights and thresholds are uncalibrated research values.

## In progress

- Live Layer 1 Fyers completeness is implemented; see `docs/plans/TASK_LEDGER.md`.
- Select and verify a point-in-time historical option-chain source before
  claiming full Phase 1 replay coverage for options structures.

## Not yet confirmed

- Production-grade market-data source history, retention and reliability.
- Portfolio, risk gateway, OMS and trade manager.
- Layer 2 reviewed contract for consuming news event-risk states.
- Historical calibration and source quality review for sentiment/event scores.
- Observability, deployment environment, POC instrument and strategy parameters.

## Active blockers

- Live trading remains unavailable: `config/base.yaml` has unverified market
  rules, no reviewed portfolio/risk/order path exists, and no point-in-time
  historical option-chain source has been chosen.
- Eight deferred safety invariants remain unproven until their components exist.

## Next action

Evaluate historical option-chain sources on history depth, point-in-time
guarantees and cost. Live recording on the VM can accumulate forward history.

## Update rules

- Keep this file below 120 lines.
- Record facts with file/test evidence; do not paste logs or plans.
- Update only after a meaningful milestone or blocker change.
- The active plan owns task detail; this file owns current project truth.
