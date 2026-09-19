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
| IV surface | P1 | Observed per-strike IVs on the chain (min 4 points) | Skip surface ranking. `DATA_GAP`. Do not interpolate |
| IV skew | P1 | 25Δ put IV − 25Δ call IV when both wings exist | Skip skew tilt. Do not invent a smile |
| Term structure | P1 | ATM IV on 2+ expiries | Skip term tilt. Single expiry is absent, not a curve |
| Realized volatility | P1 | `build_market_state` from completed 5m bars | Router already fail-visibles warmup; no invented RV |
| Greeks | P1 | Fyers chain greeks (`fyers_chain`) | Binders still require delta+IV for option families; extra greeks unused if absent |
| Depth | P1 | Bid/ask size; CAS depth features | Skip depth ranking. `cas_microstructure` stays blocked (`block_families`) |

## Already present vs newly gated

Already present before this contract: L1 quotes/chain, snapshot freshness,
identification OI/spread/greeks filters, Layer 2 quote bundles, event-risk,
synthetic paper margin, reconcile/freeze (PAPER-009).

Newly explicit: closed P0/P1 lists + SLAs in config; P0 assessment on the
paper execute path and Layer 2 when `paper_requirements` is set; observed P1
surface/skew/term/depth ranking with an absent-not-invented audit on
`SetupFeatures`.

## Policy

- LIVE stays off. Families are not auto-ENABLED.
- CAS remains SHADOW and is policy-blocked when depth is absent.
- Thresholds live in `config/paper_data.yaml`, not in domain literals.
