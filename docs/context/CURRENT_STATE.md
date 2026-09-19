# Current State

LAST_UPDATED: 2026-09-19
CURRENT_MILESTONE: Phase 4–6 - Unattended PAPER session
STATUS: PAPER_SESSION_READY_PROMOTION_BLOCKED

## Confirmed decisions

- Deterministic code owns live signals, risk, orders and protection.
- Broker state is external truth; Layer 2 owns risk and execution.
- Layer 3 emits `TradeIntent` only; Layer 4 is asynchronous/proposal-only.
- Weekly AI may propose strategy-family stances only. It never sizes, stops or
  submits. No LLM call on the live path.
- Forward paper is the evidence path. Real-money use needs a signed promotion.

## Implemented

- Deterministic identification stack (`market_state`, contract binders,
  structure router) wired into `trading paper session` via
  `config/identification.yaml`.

- `config/paper.yaml` (`Environment.PAPER`, `ACC-PAPER-1`). `base.yaml` is still
  BACKTEST.
- Unattended `trading paper session`: Telegram Fyers login, live L1, master-backed
  option/future candidates, news `EventRiskState`, five strategies, paper OMS,
  exits, Telegram post-trade/EOD, `data/paper/cohorts/` (PAPER-003).
- PAPER positional fills persist frozen exit policy and restore it on restart
  against paper broker state before new entries (PAPER-006). Protective STOP
  stubs remain local software coverage, not broker-resident orders. Debit-spread
  exits keep frozen `LEG_PRICE` on the strategy monitor long (not first
  `position.legs[0]`, not remapped to `STRATEGY_PNL`). Iron condors stay
  `STRATEGY_PNL`.
- Paper broker synthetic margin for live weekly symbols. Isolation still refuses
  Fyers transaction adapters (PAPER-004).
- Layer 4 scorecard/eligibility CLI; weekly agent ships `enabled: false`.

## Verification

- `uv run ruff check .` and `uv run mypy --strict` are required after this
  change.
- Offline tests include `tests/test_paper_session.py`,
  `tests/test_paper_lifecycle.py` and `tests/test_candidates.py`.

## Blocking gaps

- Unverified `charges_per_lot` keeps net expectancy `None` and eligibility
  `INELIGIBLE`.
- LIVE `config/base.yaml` market-rule values remain unverified.
- PAPER-005 (real-capital promotion) is out of scope until evidence and charges
  exist.
- CAS still needs a live 15:00–15:30 IST window on real depth.

## Next action

Sunday: `trading data backfill instruments`. Monday: start
`trading paper session` (Telegram OAuth is the only human step).

## Update rules

- Keep this file below 120 lines.
- Record facts with file/test evidence; do not paste logs or plans.
- Update only after a meaningful milestone or blocker change.
