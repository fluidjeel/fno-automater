# Gate G1 — One-Lot Feasibility and Affordability Report

> [!WARNING]
> **Offline Feasibility Only — NOT Permission to Run PAPER**  
> An `AFFORDABLE` verdict means one complete structure fits within the mode's per-trade loss cap at current lot size and premiums. It is **not** an authorization to trade. Per Gate G2, a strategy family cannot receive a `PAPER` stance until entry, partial-fill repair, monitoring, exit, and restart lifecycle evidence is proven.

**Date:** 2026-09-24  
**Spec Version:** v1.1 ([`NIFTY_FOUR_MODE_CURSOR_REDESIGN.md`](NIFTY_FOUR_MODE_CURSOR_REDESIGN.md) §31.3)  
**Requirement:** R-015, T31  
**Underlying:** NSE:NIFTY50-INDEX (Spot: 23346.4)  
**Current Instrument Master Lot Size:** 65 (from `data/reference/instruments/NSE_FO.jsonl`)  
**Total Reference Paper Equity:** INR 700000  
**Charges Source:** `config/risk.yaml` (Slippage Buffer: 0.25%)  

## 1. Chain Provenance & Verification Metadata

> [!IMPORTANT]
> **Market Status:** CLOSED_OFFLINE_SNAPSHOT  
> **Provider:** fyers  
> **Source Capture File:** `data/raw/fyers/2026-09-20/a3353b408bd2d743.json` (Capture ID: `a3353b408bd2d743`)  
> **Provider Event Time:** 2026-09-20T07:17:50.071593+00:00  
> **Receive Time:** 2026-09-20T07:17:50.071593+00:00  
> **Live Probe:** `False` (Offline snapshot; market closed).  
> **Option Expiry Used:** 22-09-2026  

## 2. Mode Capital Allocation & Sizing Limits (§10.1)

| Mode | Share | Reference Capital | Max Loss / Trade (%) | Per-Trade Loss Cap | Max Open Loss Cap | Daily Budget Cap |
|---|---:|---:|---:|---:|---:|---:|
| `M1_CAS` | 10% | INR 70000.00 | 5.0% | INR 3500.0000 | INR 7000.0000 | INR 10500.0000 |
| `M2_DIRECTIONAL` | 28% | INR 196000.00 | 4.0% | INR 7840.0000 | INR 11760.0000 | INR 15680.0000 |
| `M3_TACTICAL_POSITIONAL` | 30% | INR 210000.00 | 2.0% | INR 4200.0000 | INR 8400.0000 | INR 10500.0000 |
| `M4_STRATEGIC_POSITIONAL` | 32% | INR 224000.00 | 1.0% | INR 2240.0000 | INR 6720.0000 | INR 8960.0000 |

## 3. Structure Affordability Matrix

Sizing includes defined loss for 1 lot, slippage buffer fraction, and the single Layer 2 risk policy charges schedule (₹50 base per leg-pair).

| Mode | Family ID | Structure / Strikes | Legs | Points | Defined Loss (INR) | Total Cost (INR) | Trade Cap | Status | One-Lot Fits Cap |
|---|---|---|:---:|---:|---:|---:|---:|---|:---:|
| `M1_CAS` | `long_call` | Buy 23500 CE @ ask 30.15 (delta 0.24) | 1 | 30.15 | 1959.75 | 2014.65 | 3500.00 | **`AFFORDABLE`** | YES |
| `M1_CAS` | `long_put` | Buy 23150 PE @ ask 28.00 (delta -0.2) | 1 | 28.00 | 1820.00 | 1874.55 | 3500.00 | **`AFFORDABLE`** | YES |
| `M2_DIRECTIONAL` | `long_call` | Buy 23350 CE @ ask 88.55 (delta 0.51) | 1 | 88.55 | 5755.75 | 5820.14 | 7840.00 | **`AFFORDABLE`** | YES |
| `M2_DIRECTIONAL` | `long_put` | Buy 23350 PE @ ask 84.60 (delta -0.49) | 1 | 84.60 | 5499.00 | 5562.75 | 7840.00 | **`AFFORDABLE`** | YES |
| `M3_TACTICAL_POSITIONAL` | `bull_call_debit` | Buy 23550 CE @ 19.70 / Sell 23600 CE @ 12.40 | 2 | 7.30 | 474.50 | 525.69 | 4200.00 | **`AFFORDABLE`** | YES |
| `M3_TACTICAL_POSITIONAL` | `bear_put_debit` | Buy 23200 PE @ 37.00 / Sell 23150 PE @ 27.90 | 2 | 9.10 | 591.50 | 642.98 | 4200.00 | **`AFFORDABLE`** | YES |
| `M3_TACTICAL_POSITIONAL` | `bull_put_credit` | Sell 23600 PE @ 257.35 / Buy 23550 PE @ 215.45 | 2 | 8.10 | 526.50 | 577.82 | 4200.00 | **`AFFORDABLE`** | YES |
| `M3_TACTICAL_POSITIONAL` | `bear_call_credit` | Sell 23150 CE @ 230.45 / Buy 23200 CE @ 190.30 | 2 | 9.85 | 640.25 | 691.85 | 4200.00 | **`AFFORDABLE`** | YES |
| `M4_STRATEGIC_POSITIONAL` | `bull_call_debit` | Buy 23550 CE @ 19.70 / Sell 23600 CE @ 12.40 | 2 | 7.30 | 474.50 | 525.69 | 2240.00 | **`AFFORDABLE`** | YES |
| `M4_STRATEGIC_POSITIONAL` | `bear_put_debit` | Buy 23200 PE @ 37.00 / Sell 23150 PE @ 27.90 | 2 | 9.10 | 591.50 | 642.98 | 2240.00 | **`AFFORDABLE`** | YES |
| `M4_STRATEGIC_POSITIONAL` | `bull_put_credit` | Sell 23600 PE @ 257.35 / Buy 23550 PE @ 215.45 | 2 | 8.10 | 526.50 | 577.82 | 2240.00 | **`AFFORDABLE`** | YES |
| `M4_STRATEGIC_POSITIONAL` | `bear_call_credit` | Sell 23150 CE @ 230.45 / Buy 23200 CE @ 190.30 | 2 | 9.85 | 640.25 | 691.85 | 2240.00 | **`AFFORDABLE`** | YES |
| `M4_STRATEGIC_POSITIONAL` | `short_iron_condor_defined` | 23150/23200 PE + 23450/23500 CE | 4 | 26.95 | 1751.75 | 1856.13 | 2240.00 | **`AFFORDABLE`** | YES |
| `M4_STRATEGIC_POSITIONAL` | `short_iron_butterfly_defined` | 23300 PE buy, 23350 PE/CE sell, 23400 CE buy | 4 | 6.35 | 412.75 | 513.78 | 2240.00 | **`AFFORDABLE`** | YES |
| `M4_STRATEGIC_POSITIONAL` | `long_call_butterfly` | Buy 23300 CE, Sell 2x 23350 CE, Buy 23400 CE | 4 | 6.20 | 403.00 | 504.01 | 2240.00 | **`AFFORDABLE`** | YES |
| `M4_STRATEGIC_POSITIONAL` | `long_put_butterfly` | Buy 23300 PE, Sell 2x 23350 PE, Buy 23400 PE | 4 | 6.05 | 393.25 | 494.23 | 2240.00 | **`AFFORDABLE`** | YES |
| `M4_STRATEGIC_POSITIONAL` | `long_straddle` | Buy 23350 CE @ 88.55 + Buy 23350 PE @ 84.60 | 2 | 173.15 | 11254.75 | 11332.89 | 2240.00 | `MIN_LOT_EXCEEDS_BUDGET` | NO |
| `M4_STRATEGIC_POSITIONAL` | `long_strangle` | Buy 23150 PE @ 28.00 + Buy 23500 CE @ 30.15 | 2 | 58.15 | 3779.75 | 3839.20 | 2240.00 | `MIN_LOT_EXCEEDS_BUDGET` | NO |

## 4. Key Findings & Policy Directives

1. **Mode 1 (CAS):** Moderately OTM single-leg options in the policy delta band [0.20, 0.35] cost between ₹1,874.55 and ₹2,014.65 per 65-contract lot, fitting within the ₹3,500.00 per-trade cap (`AFFORDABLE`). ATM single-legs are rejected by strike policy.
2. **Mode 2 (Directional):** The 0.45–0.65 absolute-delta band is unchanged. Cheapest in-band one-lot cost on this chain is ₹5,562.75 against a per-trade cap of ₹7,840.00. That cost fits the revised allocation. A later premium that pushes all-in cost above the cap returns `MIN_LOT_EXCEEDS_BUDGET`. Do NOT cut delta to force a pass.
3. **Mode 3 (Tactical Spreads):** All four vertical spreads (50-point width) cost between ₹525.69 and ₹691.85 per lot, well within the ₹4,200.00 per-trade cap (`AFFORDABLE`).
4. **Mode 4 (Strategic Positional Basket):**
   - **Fits Cap (8 structures):** Verticals, short iron condor, short iron butterfly, long call butterfly, and long put butterfly fit comfortably under the ₹2,240.00 cap.
   - **Exceeds Budget:** `long_straddle` costs ₹11,332.89 (exceeding ₹2,240.00 cap by 5.1x) -> `MIN_LOT_EXCEEDS_BUDGET`.
   - **Exceeds Budget:** `long_strangle` costs ₹3,839.20 (exceeding ₹2,240.00 cap by 1.7x) -> `MIN_LOT_EXCEEDS_BUDGET`.
   - **Binding Rule:** As mandated by §10.1 and §31.3, structures that exceed the budget **cannot receive a PAPER stance** and remain non-executable in configuration. Lot size cannot be split and caps cannot be artificially raised.
5. **Cash and Margin Headroom:** For credit and condor structures, defined loss plus slippage and charges satisfies the per-trade risk bound. Temporary margin headroom required prior to long protection fill is documented and will be bounded by P2 margin ledgers.

## 5. Gate G1 Sign-Off Status

- [x] Read lot size dynamically from instrument master (`NSE_FO.jsonl` -> 65 contracts).
- [x] Quoted chain loaded dynamically with verified timestamps and offline disclaimer.
- [x] Policy strike scanners scan mandate delta/width bands rather than static strike constants.
- [x] M2 directional mandate enforced without delta dilution.
- [x] Non-executable status bound to `MIN_LOT_EXCEEDS_BUDGET` with zero approved lots.
- [x] Offline feasibility explicitly decoupled from PAPER stance authorization.
