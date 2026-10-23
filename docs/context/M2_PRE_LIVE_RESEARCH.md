# M2 pre-live research (not promoted)

**Status:** Research and paper evidence only. **M2 live paper remains Nifty-only**
until this checklist is satisfied and config promotion is explicit.

**Findings log:** [`M2_PAPER_RESEARCH_NOTES.md`](M2_PAPER_RESEARCH_NOTES.md)  
**Working branch:** `m2-enhancements` (code + `scripts/m2_*.py` sweeps)

---

## What we already know (Oct 2026)

- Direction-only universe rank favors different names than a low-Nifty-correlation screen.
- Oracle paper worst M2 losses in mid-Oct were **~₹4.5–4.8k net**, mostly **35% premium stops**.
- A **~₹22k “9 Oct” session loss** does not match **closed-trade** P&L; fill-day sums mislead.
- Stock candidates (e.g. BHARTIARTL) can be liquid but **weak on trend-strength rank**.
- Long four-mode scans required **PR #55** so protection heartbeats do not trip the watchdog.

---

## Edge measurement (22 Oct 2026)

Read-only. No config change. Paper book is still Nifty-only.

History is 5–22 Oct 2026 (19 closed trades). There is no later month to hold out.
The decision window is **5–16 Oct** (15 trades, ledger **₹18,028**). **19–22 Oct**
was not used to pick anything (4 trades, ledger **−₹3,497**). 20 Oct has almost
no saved chain. Re-run with `scripts/m2_edge_checks.py`. Cutoffs were fixed
first: a 0.10% move in 15 minutes, implied volatility more than **1.3×** recent
realized volatility, and a stop of **one 14-bar range** on the index.

### What the sweeps already on this branch showed (5–21 Oct)

Ledger for that shorter window: **₹20,164** on 18 trades.

- Eight days moved at least 0.45%. M2 was on the right side and profitable on
  **2 of 8**. The other six were flat, mixed, or the wrong way.
- Letting the direction score through at 0.25, 0.20, or 0.15, while still
  requiring the last hour to agree, **reduced** net (best of those was ₹18,509
  against ₹20,164) and added more losing trades than winning ones.
- Keeping the same trades overnight, with no end-of-day flatten, turned
  **₹20,164 into −₹439**. Widening that overnight loss limit still finished
  near **₹10,000**, about half the same-day book.
- Flattening the seven end-of-day exits at 14:00 instead of 15:00 looked
  **₹3,364** better. That clock was chosen by looking at the same trades, so
  it is a hint, not a result.
- The pre-close early-exit study has no finished trades to compare.

On the big days, the hour rule usually fired **8–30 minutes** after Nifty had
already moved 0.15% from the open. On 15 Oct and 21 Oct it never lined up with
the day’s direction. The 15-minute signal was only a little earlier than the
hour on the days both fired.

On **7 of 19** trades the stored family said “call” when the filled contract
was a put, or the reverse. This study followed the contract.

### The three checks

| Check | 5–16 Oct net | Versus ledger | Worst loss | 19–22 Oct net |
| --- | --- | --- | --- | --- |
| Ledger | ₹18,028 | — | −₹4,767 | −₹3,497 |
| Enter on the 15-minute move, without waiting for the hour | ₹30,662 | **+₹12,633** | −₹4,767 | ₹278 |
| Skip the buy when implied vol is above 1.3× recent realized vol | ₹20,023 | +₹1,995 | −₹4,767 | −₹4,435 |
| Stop when the index moves one recent 14-bar range against the trade | ₹26,444 | **+₹8,416** | **−₹2,489** | **₹2,220** |

The pre-declared rule (higher fit-window net, worst loss not larger) picks
**the earlier entry**. That lead is not clean enough to trade. Of the
**₹12,633**, **₹5,890** is one added trade on 13 Oct that reached the 80%
target in about a minute. The rest is mostly a 16 Oct call that replaced
trades the book took later that morning. Seven trades were added and three
book trades were replaced. The worst loss did not shrink. Holdout improved,
on four trades.

Skipping an expensive option added little on the fit window and **lost** on
the holdout. It also stood aside on more of the large days. Not the edge.

The index-range stop is the clean comparison: same entries, no new trades.
Eight trades exited sooner. Fit-window losers were held about **249 minutes**
instead of **546**. The worst fit loss fell from ₹4,767 to ₹2,489, and the
holdout’s worst loss fell from ₹5,632 to ₹2,671. This is the check to keep
watching. It is still one fortnight of chain bids, not a change to the live
stop.

### Stocks and broker limits — not started

No check is strong enough to add names. M2 stays Nifty-only.

When a check does survive a later month, stocks are not a copy of these
settings. This book held options with **3–8 days** left, mostly the weekly
contract. Stock options are monthly, so “next week, skip today and tomorrow”
is the wrong clock. The test is whether one day of decay is small next to the
move, on that option. Stops have to use **that name’s** own range. A name
whose one lot costs more than the 2% trade guide is not a candidate. A name
that moves with Nifty is the same bet as the book we already have.

Broker caps, already hit once: about **10 calls a second**, **200 a minute**,
**50 symbols** per quote, **50 strikes** per chain. Breaking the minute cap
more than three times can block the account for the day. Five extra names fit
in **one** quote call a minute for the underlying. An option chain is requested
only after that name’s signal, and only the strikes in the delta band. Open
positions keep the existing quote poll. The 213-name list stays an overnight
screen, not a live scan.

---

## Open questions before any stock M2 or STRICT promotion

Answers from the 22 Oct measurement are in the section above. Stocks are not approved.

| Area | Question | Suggested tool / data |
| --- | --- | --- |
| Universe | Final list: ER-only top names vs composite screen vs Nifty-only | `scripts/m2_*`, NSE lots + EOD |
| Liquidity | Strike-level OI, spread %, delta ladder on Fyers for finalists | Oracle Fyers pull |
| Direction | Walk-forward `trend_score` per name vs Nifty M2 machinery | Paper DISCOVERY replay |
| Exits | Stop width, EOD flatten time, pre-close early exit (A/B study) | `m2_stop_width_sweep`, `m2_eod_flatten_time_sweep`, `m2_ab_study` |
| Carry | Overnight carry vs same-day only; gate thresholds | `carry_gate.yaml`, `m2_positional_no_eod_replay` |
| Data | Rate limits if multi-symbol chain + protection REST | `data_pipeline.yaml`, Fyers caps |
| Risk | One open trade per mode, campaign loss caps with stock margin | Paper book + Layer 2 limits |
| Stress | Event windows (budget, RBI, expiry week) on finalists | `m2_big_move_catch_analysis` |

---

## Paper gates (suggested, not policy yet)

1. **Minimum sessions** in DISCOVERY with stock bindings disabled vs enabled (shadow).
2. **No watchdog restarts** during long scans with open positions (post #55).
3. **Documented max loss** per trade and per day within campaign caps.
4. **Out-of-sample month** not used to tune thresholds in `discovery.yaml`.

---

## Repo layout (m2-enhancements)

| Path | Role |
| --- | --- |
| `scripts/m2_big_move_catch_analysis.py` | Nifty move days vs M2 exposure |
| `scripts/m2_fallback_threshold_sweep.py` | Direction fallback threshold |
| `scripts/m2_stop_width_sweep.py` | Premium stop fractions |
| `scripts/m2_eod_flatten_time_sweep.py` | Time exit sweep |
| `scripts/m2_positional_no_eod_replay.py` | Carry / no-EOD scenarios |
| `scripts/m2_edge_checks.py` | Earlier entry, rich-premium skip, index-range stop |
| `config/m2_ab_study.yaml` | Mode A vs B shadow early exit |
| `src/trading/runtime/m2_ab_study.py` | Study runtime hook |

---

## Oracle paper data

- Book: `data/paper/trading.sqlite`
- Raw Nifty samples: `data/raw/fyers/YYYY-MM-DD/`
- Do **not** treat DISCOVERY paper autonomy as approval for LIVE or multi-stock without
  updating `docs/context/` and validated config.
