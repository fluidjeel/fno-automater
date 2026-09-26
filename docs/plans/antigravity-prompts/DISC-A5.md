# DISC-A5 — Staleness from quote time (Antigravity agent)

You are in worktree branch `disc-a5` only. Implement ONE story: DISC-A5.

## Read first
1. `docs/plans/DISCOVERY_MODE_STORIES.md` section DISC-A5
2. `docs/context/DISCOVERY_MODE.md`
3. `config/discovery.yaml`
4. `.cursor/rules/00-core.mdc`

## Do
- Strategies measure age from freshest quote/chain time (`calculation_time` / `quote_time`), NOT bar `event_time`.
- Move `MAX_SNAPSHOT_AGE_SECONDS` to config (STRICT keeps 120s / CAS 30s).
- DISCOVERY hard rule: `hard_quote_max_age_ms` (5 minutes). Between strict limit and 5m = SOFT DATA_STALE tagged.
- Fix bar-event_time-as-freshness for BOTH profiles (measurement bug).

## Acceptance tests required
- Quote 20s old, bar 4m old → M4 does NOT DATA_STALE under either profile
- Quote 3m old → DISCOVERY soft DATA_STALE; STRICT rejects
- Quote 6m old → both HARD reject

## Out of scope
DISC-A2/A3/A4/A6+. No OMS/broker decision edits. No LIVE. No git reset of other branches.

## Land
Commit on `disc-a5` with message naming DISC-A5. Run focused tests + ruff/mypy on touched paths. Do not merge to main.
