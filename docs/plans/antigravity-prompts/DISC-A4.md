# DISC-A4 — touch-v1 paper fills + strict verdict (Antigravity agent)

You are in worktree branch `disc-a4` only. Implement ONE story: DISC-A4.

## Read first
1. `docs/plans/DISCOVERY_MODE_STORIES.md` section DISC-A4
2. `docs/context/DISCOVERY_MODE.md`
3. broker/paper/adapter.py, analytics/fills, paper_runner model check

## Do
- Add touch-v1: BUY at ask, SELL at bid, plus charges_per_lot; no depth/trade-through
- Missing bid/ask → PRICE_UNAVAILABLE HARD
- DISCOVERY: fill with touch-v1 AND shadow conservative-v1 → store strict_fill_verdict
- paper_runner accepts touch + shadow pair
- Protection-first multi-leg unchanged
- Cohort fill_model_version = touch-v1 (scorecard won't pool with conservative-v1)
- STRICT still conservative-v1 alone

## Acceptance tests required
As in DISCOVERY_MODE_STORIES.md DISC-A4.

## Out of scope
DISC-A2/A3/A5/A6+. No LIVE gateway soft rules.

## Land
Commit on `disc-a4` naming DISC-A4. Focused tests + ruff/mypy. Do not merge to main.
