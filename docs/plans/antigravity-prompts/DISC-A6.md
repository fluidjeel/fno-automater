# DISC-A6 — M2 direction fallback (Antigravity agent)

You are in worktree branch `disc-a6` only. Implement ONE story: DISC-A6.

## Read first
1. `docs/plans/DISCOVERY_MODE_STORIES.md` section DISC-A6
2. `docs/context/DISCOVERY_MODE.md`
3. `config/discovery.yaml` (direction/selection sections)
4. binders.py / market_state.py / long_option.py / four_mode_producers (narrow)

## Do
- DISCOVERY direction fallback from 15m+60m returns when trend not UP/DOWN; tag DIRECTION_FALLBACK
- Soft WARMUP_INCOMPLETE when warmup incomplete
- Two-pass contract pick; delta from config; tag M2_DELTA_FALLBACK
- Strike feed / expiry fallback from discovery config; hard min 2 DTE
- Pass binder reason_codes through (no false INSTRUMENT_UNKNOWN)
- DIRECTION_UNRESOLVED instead of misnamed PRICE_UNAVAILABLE when direction None
- long_option silent abstains → DIRECTION_NEUTRAL / OPTION_TYPE_MISMATCH

## Acceptance tests required
As listed in DISCOVERY_MODE_STORIES.md DISC-A6 (fixture +150pt rise, delta fallback, expiry, binder reasons).

## Out of scope
DISC-A2 sizing, A4 fills, A5 staleness, A7+. No LIVE.

## Land
Commit on `disc-a6` naming DISC-A6. Focused tests + ruff/mypy. Do not merge to main.
