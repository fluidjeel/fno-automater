# Current State

LAST_UPDATED: 2026-09-20
CURRENT_MILESTONE: Agent Desk Stage D — authority ladder (D1–D2 DONE; D3 READY)
STATUS: P0_HARDENED_P1_SELECTION_UNATTENDED_NOT_LIVE_SAFE

## Confirmed decisions

- Stage D: C1 keeps BOUNDED = config-promotion only.
- **ADESK-D1 DONE:** ConfidenceBucket + Phase-1 downscale sizing (le=1 by type).
- **ADESK-D2 DONE:** FRAGILITY + POSTTRADE ADVISORY via signed AuthorityGrant;
  demote to OBSERVE without grant; Telegram builders do not send.
- Stage C complete; no LLM on live/terminal path.

## Implemented (Agent Desk)

- D1: `confidence_sizing.py`, `tests/test_confidence_sizing.py`.
- D2: grant wiring in `ai/fragility.py`, `ai/posttrade.py`;
  `tests/test_advisory_promotion.py`.

## Verification

- D1 focused: 36 passed. D2 focused: 9 passed
  (`test_advisory_promotion` + fragility/posttrade desks).

## Blocking gaps

- 60s software-only poll — not live-safe.
- LIVE market-rule values unverified; CAS live window still needed.

## Next action

Implement **ADESK-D3**: PORTFOLIO BOUNDED for config-promotion proposals only.

## Update rules

- Keep this file below 120 lines.
- Record facts with file/test evidence; do not paste logs or plans.
