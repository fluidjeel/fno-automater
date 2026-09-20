# PAPER two-tier market-data contract

`config/paper_data.yaml` is the closed P0/P1 matrix. Paper entry loads it in
`trading paper session` and fails closed on any P0 breach. P1 series improve
strike/structure ranking only when observed; missing series are logged and
never filled.

## Matrix

| Field | Tier | Source | Gate when missing / stale / zero-invalid |
| --- | --- | --- | --- |
| LTP | P0 | Fyers quote / chain last (`MarketQuote.last`) | Block entry. `PRICE_UNAVAILABLE` |
| Bid/ask | P0 | Fyers quote / chain book (`bid`, `ask`) | Block entry. `PRICE_UNAVAILABLE` |
| Quote timestamp + freshness | P0 | `SnapshotTimes.event_time` vs `max_age_ms` (120s, same as `paper.yaml` quote SLA) | Block entry. `DATA_STALE` / `SNAPSHOT_MISMATCH` |
| Volume | P0 | Quote or chain `volume` | Block if missing. Zero is a valid observation |
| Open interest | P0 | Chain `oi` on the traded derivative | Block. `DEPTH_INSUFFICIENT` (zero-invalid) |
| Contract metadata | P0 | Fyers symbol master → `ContractRef` + lot size | Block. `INSTRUMENT_UNKNOWN` |
| Margin estimate | P0 | Paper broker `preview_margin` at Layer 2 | Block. `MARGIN_INSUFFICIENT` |
| Position + broker state | P0 | Paper broker + `PortfolioSnapshot` / reconcile | Block. `RECONCILIATION_UNRESOLVED` |
| Event state | P0 | `collect_event_risk` → `EventRiskState` | Block if missing/stale. `EVENT_BLACKOUT` / `DATA_STALE` |
| IV surface | P1 | Observed per-strike IVs on the chain (`windows` + `min_points`) | Skip surface ranking. `DATA_GAP`. Do not interpolate |
| IV skew | P1 | 25Δ put IV − 25Δ call IV inside the configured delta band | Skip skew tilt. Do not invent a smile |
| Term structure | P1 | ATM IV on 2+ expiries (`min_points`) | Skip term tilt. Single expiry is absent, not a curve |
| Realized volatility | P1 | Completed Fyers 5m history bars via `build_market_state` | Skip RV ranking / expansion override. No invented RV |
| Greeks | P1 | Fyers chain greeks (`fyers_chain`) | Binders still require delta+IV; theta/vega ranking skipped if absent |
| Depth | P1 | Fyers REST `/data/depth` top-of-book `volume`; quote sizes if sent | Skip depth ranking. `cas_microstructure` blocked until size is observed. Exit still protects |

## P1 formulas and windows

Windows live under `config/paper_data.yaml` `windows`. `observe_p1_features`
never interpolates, forward-fills, or substitutes a sibling strike/expiry.

**IV surface.** Each chain row with converged `implied_volatility > 0`, expiry,
strike and option type is one point. Present when `len(points) >= IV_SURFACE.min_points`
(4). ATM IV for an expiry is the median IV of the nearest-to-spot observed
strikes. Ranking: long prefers `IV / ATM_IV` cheap; short prefers ~10% rich.

**IV skew.** Mean IV of puts with `|delta|` in `[delta_min, delta_max]` minus the
same for calls (`0.20`–`0.30`). Either wing missing → absent
(`missing_25d_put_or_call`). Ranking: positive skew (puts rich) tilts toward
calls.

**Term structure.** ATM IV per expiry, sorted. Present when at least
`TERM_STRUCTURE.min_points` (2) expiries have an ATM. `term_slope = front / back`.
Ranking prefers the cheaper ATM expiry. One expiry is not a curve.

**Realized volatility.** Close-to-close sample stdev on completed 5m bars:

- `short_rv = stdev(returns[-short_window_bars:])` (12 = 60 minutes)
- `long_rv = stdev(returns[-long_window_bars:])` (50, same as identification warmup)
- `rv_ratio = short_rv / long_rv`
- `annualized = long_rv * sqrt(session_bars * trading_days) * 100` (75 × 252)

Present only when `MarketState.realized_volatility_annualized > 0` and
`completed_bar_count >= long_window_bars`. Ranking: long prefers cheap
`IV / RV`; short prefers rich. Router: observed expanding RV (`VolatilityState.EXPANDING`)
prefers `debit_spread` even when IV looks cheap.

**Greeks.** Present when at least one candidate has converged delta and IV
(`windows.greeks.require_*`). Theta and vega rank relatively inside the
universe when those fields exist on the row; they are not required for
presence and are never filled.

**Depth.** Displayed `min(bid_size, ask_size)` from Fyers REST depth level
`volume` (quotes document bid/ask/volume, not size). Present when at least
`min_book_levels` symbols meet `min_top_size`. Extra REST fetches are capped
by `max_symbols` (highest OI first). Ranking is relative top size. At exit,
`observe_exit_depth` logs `symbol:reason` on `PaperRunner.exit_depth_gaps`
and does **not** block software stops (invariant 6). Conservative fills and
CAS remain depth-dependent.

## Already present vs newly gated

Already present before PAPER-010: L1 quotes/chain, snapshot freshness,
identification OI/spread/greeks filters, Layer 2 quote bundles, event-risk,
synthetic paper margin, reconcile/freeze (PAPER-009).

PAPER-010: closed P0/P1 lists + SLAs; P0 assessment on the paper execute path
and Layer 2; observed P1 surface/skew/term/depth hooks.

PAPER-011: documented P1 windows; builders that change binder ranking, router
preference, and allow-table CAS blocking; depth on entry **and** exit paths;
absence reasons on `SetupFeatures.p1_absence_reasons`.

## Policy

- LIVE stays off. Families are not auto-ENABLED.
- CAS is PAPER when observed depth is present, and policy-blocked when depth is
  absent. Incomplete CAS keys fail closed (`DATA_GAP`).
- Thresholds live in `config/paper_data.yaml`, not in domain literals.
