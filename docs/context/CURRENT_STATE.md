# Current State

LAST_UPDATED: 2026-09-20
CURRENT_MILESTONE: Agent Desk Stage A — foundations (ADESK-A1 DONE; ADESK-A2 READY)
STATUS: P0_HARDENED_P1_SELECTION_UNATTENDED_NOT_LIVE_SAFE

## Confirmed decisions

- Deterministic code owns live signals, risk, orders and protection.
- Broker state is external truth; Layer 2 owns risk and execution.
- Layer 3 emits `TradeIntent` only; Layer 4 is asynchronous/proposal-only.
- Weekly AI may propose strategy-family stances only. It never sizes, stops or
  submits. No LLM call on the live path.
- Forward paper is the evidence path. Real-money use needs a signed promotion.
- Agent Desk **BOUNDED** (C1, 2026-09-20): config-promotion only. Agents never
  get intraday live-path authority. Intraday stays L3+L2 with no LLM. BOUNDED
  may only promote/demote already-coded config (strategy-family enable/shadow/halt)
  through the existing L4 proposal path after a signed grant.

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
  stubs remain local software coverage, not broker-resident orders.
- Twice-daily NSE positional review at 10:30 and 14:30 IST (PAPER-007).
- P0 safety hardening (PAPER-009) and two-tier paper-data contract (PAPER-010/011).
- Read-only local dashboard: `trading dashboard serve|snapshot` (ADESK-A0.1).
- Layer 4 agent measurement hardening (ADESK-A0.3..A0.5): monthly budget ledger,
  resolved model id + temperature/seed persistence, separate setup vs agent Brier.
- `evaluation.yaml` `charges_per_lot.verified_at: 2026-09-19` (published schedule
  estimate; contract-note cross-check still required for LIVE).
- ADESK-A1: `AuthorityGrant` + `authority_grants` + demotion to OBSERVE. BOUNDED
  is config-promotion only; live-path actions rejected at write.

## Verification

- `uv run ruff check .` and `uv run mypy` are clean on the committed tree.
- `tests/test_dashboard.py`, `tests/test_agent_stage0.py`, `tests/test_l4_agent.py`,
  `tests/test_authority_grant.py` (C1 BOUNDED live-path reject + OBSERVE demotion),
  and the PAPER-010 focused suite pass.
- `trading dashboard snapshot` runs without `ModuleNotFoundError`.

## Blocking gaps

- 60s software-only poll cannot see intra-interval stop prints (case 8).
  SAFETY: NOT ACCEPTABLE for live unattended stops.
- LIVE `config/base.yaml` market-rule values remain unverified.
- PAPER-005 (real-capital promotion) is out of scope until live paper evidence.
- CAS still needs a live 15:00–15:30 IST window on real depth for promotion
  evidence.

## Next action

Agent Desk Stage 0 complete; **ADESK-A1 done** (AuthorityGrant + C1 demotion).
Next code is **ADESK-A2** (DecisionLog). Monday: Fyers auth, CAS depth benchmark,
supervised paper session. Do not treat software stops or the 60s poll as live-safe.

## Update rules

- Keep this file below 120 lines.
- Record facts with file/test evidence; do not paste logs or plans.
- Update only after a meaningful milestone or blocker change.
