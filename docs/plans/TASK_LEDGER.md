# Task Ledger

ACTIVE_PLAN_VERSION: 3

Use statuses `READY`, `IN_PROGRESS`, `BLOCKED`, `DONE`. Exactly one task may be
`READY` or `IN_PROGRESS`.

| ID | Status | Outcome | Scope | Verification | Dependency |
| --- | --- | --- | --- | --- | --- |
| NEWS-001 | DONE | Strict contracts, source/scoring config and taxonomy | `src/trading/news/contracts.py`, `config/news.yaml`, tests | Contract/config rejection and round-trip tests | None |
| NEWS-002 | DONE | Configured source collectors | `src/trading/news/sources.py`, offline fixtures/tests | RSS/GDELT/FRED/EIA behavior, retries, keys, range and limits | NEWS-001 |
| NEWS-003 | DONE | Normalize, deduplicate and cluster evidence | `src/trading/news/cluster.py`, tests | Stable event IDs; syndicated copies do not inflate confirmation | NEWS-001 |
| NEWS-004 | DONE | Classifier interface and optional local FinBERT | `src/trading/news/sentiment.py`, optional extra, tests | Deterministic test classifier; unavailable model yields UNKNOWN | NEWS-001 |
| NEWS-005 | DONE | Asset impacts, snapshots and event-risk state | `src/trading/news/scoring.py`, tests | Weighted deterministic scoring, contradiction flags, no position mutations | NEWS-003, NEWS-004 |
| NEWS-006 | DONE | Store, CLI and operator documentation | `src/trading/news/storage.py`, `src/trading/cli.py`, docs | Idempotent JSONL; CLI smoke; full test/lint/type checks | NEWS-002, NEWS-005 |
| NEWS-007 | DONE | Grounded weekly proposal contract and safe default | `src/trading/news/proposal.py`, tests | Strict evidence/expiry contract; generator abstains | NEWS-005 |
| L1-001 | DONE | Live Fyers depth, status, chain Greeks, history OI | `src/trading/data/fyers/client.py`, `normalize.py`, fixtures | Offline fixtures; pipeline persists new event types | None |
| L1-002 | DONE | Session, warmup, drift, cross-source quality | `src/trading/data/quality.py` | Invariant-6 tests | L1-001 |
| L1-003 | DONE | Snapshot features: Greeks, OI, PCR, depth sizes | `src/trading/data/snapshot_builder.py` | Fixture snapshot carries ATM delta and bid size | L1-001 |
| L1-004 | DONE | Parquet/DuckDB catalog | `src/trading/data/storage/catalog.py` | DuckDB count matches JSONL event ids | L1-001 |
| L1-005 | DONE | WS daemon + systemd unit | `src/trading/data/fyers/ws.py`, `deploy/data-tick.service` | Offline reconnect test | L1-001 |

Completion record:

```text
Live Layer 1 completeness: REST depth/status/Greeks/OI, quality gates,
snapshot features, Parquet/DuckDB catalog, WS daemon. Historical option-chain
vendor source remains the Phase 1 replay blocker for options structures.
```
