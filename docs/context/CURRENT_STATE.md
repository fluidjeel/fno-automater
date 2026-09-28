# Current State

LAST_UPDATED: 2026-09-28
CURRENT_MILESTONE: PAPER Discovery Mode (`docs/context/DISCOVERY_MODE.md`)
STATUS: DISC-A0..A11_DONE; DISC-A13_DONE; DISC-A14_DONE (margin soft-resize,
safe projection, evaluation-error defense); DISC-A15_DONE (quote freshness from
fetch/calculation time, not bar event_time); DISC-A16_DONE (CAS depth dedup,
DUPLICATE quality flag, top5 imbalance fix, multi-symbol health.json);
DISC-A17 monthly chain fetch (M3/M4 20-35 DTE); DISC-A19_DONE (exit quote
publish/seed, DISCOVERY protection staleness fix, partial-leg exits,
`trading ops retry-stuck-paper-exits`); DISC-A20_DONE (protection monitor WS
handler + REST fallback for held-leg quotes; heartbeat `last_quote_at`);
DISC-A21_DONE (DISCOVERY premium-scaled structure exits: debit/credit fractions,
mid-mark + 2-quote confirm, HWM trail persisted, per-leg disaster backstops,
all-legs-fresh REST refresh, shorts-first sequencing; STRICT unchanged); Oracle
rsync + restart still pending (DISC-A12)

## Evidence labels

- TEST-PROVEN: 1950+ pytest including discovery suite (`tests/test_disc_*.py`,
  `tests/test_cohort_eod_duplicate_signals.py`).
- DEPLOYED-PAPER: Oracle still on pre-review tree until rsync; local config has
  `entry_profile: DISCOVERY`, `experiment_prefix: EXP-DISC`.
- OBSERVED-IN-MARKET: not yet.

## Discovery profile (local config)

| Item | Value |
| --- | --- |
| `entry_profile` | `DISCOVERY` |
| `experiment_prefix` | `EXP-DISC` |
| Books | Four independent ₹7L per mode; daily compounding from prior realised net |
| Fills | `touch-v1` with `conservative-v1` shadow verdict |
| Straddle/strangle | `PAPER` under DISCOVERY |
| Calendars | `SUSPENDED` (dual-expiry lifecycle unproven; no change in A13) |
| Monthly chain | Supplemental fetch for 20-35 DTE expiries (`monthly_chain=1`); TTL-cached REST; M3/M4 binders only |
| Capital caps in DISCOVERY | Soft only — downsize and/or `strict_would_block`; never reject |
| Margin in DISCOVERY | Soft only — min 1 lot; `MARGIN_INSUFFICIENT` / `MARGIN_OVERSUBSCRIBED` shadowed; projection clamped |
| Protection quotes | WS push for held-leg symbols; REST fallback batched every 2s (`rest_poll_seconds`); heartbeat `last_quote_at` |
| Structure exits (DISCOVERY) | Premium-scaled on `STRATEGY_PNL`: debit stop 35% / target 80% of paid premium; credit TP 50% / stop 2× credit (capped); mid mark + 2-quote confirm; HWM trail (+30% activate, 50% give-back); per-leg disaster backstop (−70% long); stale-leg REST refresh before eval |

## Mode stances (file and loaded)

| Mode | Config | Loaded after startup validation |
| --- | --- | --- |
| M1_CAS | PAPER | PAPER |
| M2_DIRECTIONAL | PAPER | PAPER |
| M3/M4 | PAPER | PAPER |

Routing profile is `four_mode`. **M1 remains event-only until DISC-B1** — the
60-second poll clears pending M1 events and does not submit them; only
`submit_m1_event` runs M1. M2–M4 evaluate every poll under DISCOVERY.

## Prior milestones

Four-mode redesign P1–P16 complete (P14 calendars
`EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN`). LIVE not approved.

## Follow-up (not DISC-A15)

`paper_session.py` calls `pipeline.run_once` and `feed.fetch_quotes` every cycle
on top of the timer fetch, which can 429 the following-week chain. Dedupe in a
later slice.

## Blocking gaps before Monday

1. Oracle deploy: rsync `9c9619d` + review fixes, restart `fno-paper-session`.
2. Fyers token refresh before 09:00 IST Sunday/Monday.
3. Post-deploy: startup banner `ENTRY PROFILE: DISCOVERY (temporary)`; heartbeat
   `entry_profile: DISCOVERY`; decision records per mode/family each cycle.
4. Sprint B (M1 poll, EOD discovery report, dashboard) not started.

## Next action

Deploy to Oracle with `./deploy/deploy_oracle.sh --host ubuntu@92.4.94.79
--key ../blue-green/keys/ssh-key-2026-09-12.key --with-secrets`, restart the
paper session, refresh Fyers token, and verify one full market cycle records
decisions for every routable family.
