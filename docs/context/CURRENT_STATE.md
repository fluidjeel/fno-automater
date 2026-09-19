# Current State

LAST_UPDATED: 2026-09-19
CURRENT_MILESTONE: Phase 4–6 - PAPER two-tier data contract
STATUS: P0_HARDENED_UNATTENDED_NOT_LIVE_SAFE

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
- Twice-daily NSE positional review at 10:30 and 14:30 IST (config-driven)
  decides HOLD / TIGHTEN_STOP / PARTIAL_EXIT / FULL_EXIT against that frozen
  policy, or emits a HEDGE/ROLL proposal that cannot auto-submit (PAPER-007).
  Missed slots run once on restart if still before EOD. Continuous software
  exits still evaluate every poll.
- P0 safety hardening (PAPER-009): Layer 2 validates multi-leg quote bundles
  without copying `intent.snapshot_id` onto option legs. Entry freeze
  (`entries_blocked` + reason) is persisted and restored on restart.
  Missing monitor marks `UNPROTECTED_POSITION` (never silent HOLD). Stale
  quotes persist `PROTECTION_DEGRADED` and freeze entries. PAPER stops are
  not broker-resident. 60s poll gap is a measured limitation (not live-safe).
- Two-tier paper-data contract (PAPER-010): `config/paper_data.yaml` lists
  P0 (LTP, bid/ask, freshness, volume, OI, metadata, margin, broker, event)
  and P1 (IV surface/skew/term, RV, greeks, depth). Paper session + Layer 2
  fail closed on any P0 hole. P1 ranking uses observed chain values only.
- Paper broker synthetic margin for live weekly symbols. Isolation still refuses
  Fyers transaction adapters (PAPER-004).
- Layer 4 scorecard/eligibility CLI; weekly agent ships `enabled: false`.

## Verification

- `uv run ruff check .` and `uv run mypy --strict src tests` are clean.
- PAPER-010 focused suite: `tests/test_paper_data_requirements.py` plus
  identification, gateway, paper runner/session, contracts, config, and
  PAPER-009 safety tests.

## Blocking gaps

- 60s software-only poll cannot see intra-interval stop prints (case 8).
  SAFETY: NOT ACCEPTABLE for live unattended stops.
- Unverified `charges_per_lot` keeps net expectancy `None` and eligibility
  `INELIGIBLE`.
- LIVE `config/base.yaml` market-rule values remain unverified.
- PAPER-005 (real-capital promotion) is out of scope until evidence and charges
  exist.
- CAS still needs a live 15:00–15:30 IST window on real depth.

## Next action

Sunday: `trading data backfill instruments`. Monday: supervised
`trading paper session` (Telegram OAuth is the only human step). Do not
treat software stops or the 60s poll as live-safe.

## Update rules

- Keep this file below 120 lines.
- Record facts with file/test evidence; do not paste logs or plans.
- Update only after a meaningful milestone or blocker change.
