# Current State

LAST_UPDATED: 2026-09-26
CURRENT_MILESTONE: PAPER Discovery Mode (`docs/context/DISCOVERY_MODE.md`)
STATUS: DISC-A8_DONE (DISCOVERY within-mode exact duplicates HARD; cross-mode overlap/conflict SOFT; per-mode daily entry and open-position caps; router cooldown SOFT; arbiter reason codes preserved in paper_runner)

## Evidence labels

- TEST-PROVEN: local pytest including `tests/test_m1_paper_session_integration.py`.
- DEPLOYED-PAPER: Oracle rsync tree; startup keeps `M1_CAS: PAPER`.
- OBSERVED-IN-MARKET: not yet. Market was closed; no qualifying live signal captured.

## Mode stances (file and loaded)

| Mode | Config | Loaded after startup validation |
| --- | --- | --- |
| M1_CAS | PAPER | PAPER |
| M2_DIRECTIONAL | PAPER | PAPER |
| M3/M4 | PAPER | PAPER |

Routing profile is `four_mode`. The 60-second poll clears pending M1 events and does not submit them. Entries use `submit_m1_event`.

## M1 latency policy (PAPER only)

Thresholds remain predeclared: quote age 500 ms, decision 2000 ms, execution 2000 ms, exit gap 2000 ms. Samples are recorded when provider timestamps exist. A missing or failing report emits a startup warning and runtime log line, but does not demote M1 to SHADOW. LIVE eligibility is unchanged.

Scan windows: 09:20-15:00 continuous and 15:00-15:25 closing context. Cash auction 15:30-15:40 is excluded.

## M2 allocation (capital event)

Reference equity ₹7,00,000. M2 share 28%, per-trade 4%, cap ₹7,840. M4 share 32%, per-trade cap ₹2,240. Percentage order M1 5% > M2 4% > M3 2% > M4 1%. Open-risk sum ₹33,880 under the ₹35,000 global cap. Sep 20 chain: 23350 CE all-in ₹5,820.14 and PE ₹5,562.75 fit the cap. Live following-week refresh still blocked on Fyers 401.

## Verification

- Oracle deploy: `ruff`, `mypy`, full pytest green on the rsynced tree.
- Startup validation on Oracle prints `M1_CAS: PAPER` with one latency-limitation warning.
- G1 report: `docs/reports/G1_ONE_LOT_AFFORDABILITY.md`.

## Blocking gaps

- LIVE not approved. Calendars stay `EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN` (P16).
- No in-market M1 trace or fill yet. Abstention-only sessions are acceptable if reasons are logged.
- Fyers token 401 prevents a fresh following-week affordability read for M2.

## Next action

During the cash session, capture feed-to-decision traces and any abstention reasons. Do not manufacture fills.
