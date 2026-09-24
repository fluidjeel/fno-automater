# Current State

LAST_UPDATED: 2026-09-25
CURRENT_MILESTONE: Four-Mode Session Integration — Oracle Deployed
STATUS: FOUR_MODE_PAPER_ROUTED_NOT_OBSERVED_IN_MARKET

## Confirmed decisions

- Binding C1: AuthorityMode.BOUNDED = config-promotion only; no LLM on live order path.
- Four-mode producers route through `paper_session.py` when `routing_profile: four_mode`.
- Legacy one-winner router preserved at `config/paper_session_legacy.yaml` for rollback.
- Calendars remain `EXPERIMENTAL_ONLY_RISK_BOUND_UNPROVEN` — off strict book.
- M1 PAPER requires `cas_event_driven.enabled: true` and latency gate pass (G3); polled loop alone is insufficient.

## Deployed (2026-09-25)

- Local commit: `47dcf15ffbdf6bb2cc0d7470c89c76746106aa1a` (rsync to Oracle; VM has no git).
- Oracle active config: `config/paper_session.yaml` ← `paper_session_four_mode.yaml`.
- Oracle integration tests: `tests/test_four_mode_session_integration.py`, `tests/test_cas_event_path.py` — green on VM.
- Instrument master backfill run on Oracle (`trading data backfill instruments`, 2026-09-24 captures).
- NIFTY lot size: **65**; NFO session `0915-1540|1815-1915` (from `NSE_FO.jsonl`).

## Session routing (active)

| Mode | Stance | Notes |
| --- | --- | --- |
| M1_CAS | SHADOW | `cas_event_driven.enabled: false` |
| M2_DIRECTIONAL | SHADOW | G1 `MIN_LOT_EXCEEDS_BUDGET` at lot 65 |
| M3_TACTICAL_POSITIONAL | PAPER | All four vertical families |
| M4_STRATEGIC_POSITIONAL | PAPER | Affordable families only; straddle/strangle SHADOW |

Rollback: `cp config/paper_session_legacy.yaml config/paper_session.yaml` on Oracle and restart `fno-paper-session`.

## Verification

- Phase P14 calendar registry + P16 doc reconciliation complete (calendars off strict book).
- `tests/test_four_mode_session_integration.py` — M3 fill + restart duplicate suppression.
- `tests/test_four_mode_trade_simulations.py` — 12 simulations (3 per mode M1-M4).
- `tests/test_cas_event_path.py` — event trigger + latency gate.
- G1 refreshed: `docs/reports/G1_ONE_LOT_AFFORDABILITY.md` (lot 65, 14/18 affordable).
- Live trading blocked; PAPER only on M3/M4 until in-market fills observed.

## Blocking gaps (honest)

- LIVE not approved. No market fills observed yet on four-mode route.
- `fno-paper-session.service` inactive outside session window (supervisor starts PRE_MARKET).
- M2 blocked by G1 until premium/capital environment changes.
- M1 blocked until event-driven path enabled and session latency tests pass.

## Next action

Observe first four-mode PAPER session at open; use dashboard funnel + `trading evaluate operator-view`. Roll back to legacy if producer errors appear in `data/paper/session.log`.

## Update rules

- Keep this file below 120 lines.
- Record facts with file/test evidence; do not paste logs or plans.
