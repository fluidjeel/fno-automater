# Current State

LAST_UPDATED: 2026-09-20
CURRENT_MILESTONE: Agent Desk Stage D — authority ladder (D1 DONE; D2 READY)
STATUS: P0_HARDENED_P1_SELECTION_UNATTENDED_NOT_LIVE_SAFE

## Confirmed decisions

- Stage D plan APPROVED (2026-09-20); C1 keeps BOUNDED = config-promotion only.
- **ADESK-D1 DONE:** ConfidenceBucket + Phase-1 downscale-only sizing; agent
  `size_multiplier` capped le=1 by type; no live BOUNDED.
- Stage C (ADESK-C1..C4) complete on main; terminal path has zero LLM.
- Deterministic code owns live signals, risk, orders and protection.
- No LLM on the live/intraday path.
- Agent Desk **BOUNDED** (C1): config-promotion only.

## Implemented (Agent Desk)

- Stage 0 / A / B / C complete on `main`.
- D1: `ConfidenceBucket` in `enums.py`; `domain/contracts/confidence_sizing.py`
  Phase-1 map; ENTRY `sizing_advice_for_bucket`; `tests/test_confidence_sizing.py`.

## Verification

- Focused: `uv run pytest tests/test_confidence_sizing.py tests/test_entry_desk.py
  tests/test_agent_decision.py` (36 passed).

## Blocking gaps

- 60s software-only poll cannot see intra-interval stop prints — not live-safe.
- LIVE market-rule values in base config remain unverified.
- CAS depth still needs a live 15:00–15:30 IST window for promotion evidence.

## Next action

Implement **ADESK-D2** only: promote FRAGILITY + POSTTRADE to ADVISORY via signed
AuthorityGrant; demote to OBSERVE without grant.

## Update rules

- Keep this file below 120 lines.
- Record facts with file/test evidence; do not paste logs or plans.
- Update only after a meaningful milestone or blocker change.
