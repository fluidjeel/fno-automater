# M2 paper research notes (Oracle, Oct 2026)

**Purpose:** Durable summary of read-only M2 / NSE F&O universe work from chat sessions
(Oct 2026). Not a live config or promotion authority — `docs/context/` strategy
and DISCOVERY rules still govern what runs in paper.

**Pre-live checklist and open questions:**
[`M2_PRE_LIVE_RESEARCH.md`](M2_PRE_LIVE_RESEARCH.md).

**Data sources used:**

- Oracle `~/fno-automated/data/paper/trading.sqlite` (paper book)
- Yahoo Finance EOD `*.NS` + NSE `fo_mktlots.csv` (universe screens)
- Fyers option chain on Oracle (liquidity snapshots)
- Raw Nifty index samples under `data/raw/fyers/` (session moves)

---

## 1. Directionality-only universe re-rank (213 stock underlyings)

**Method:** Median 20-day Kaufman efficiency ratio on daily closes (Jan 2018–Mar 2026);
NSE F&O lot file for symbol list (indices excluded).

| Rank | Symbol | Median 20d ER |
| --- | --- | --- |
| 1 | ENRIN | 0.2877 |
| 2 | DIXON | 0.2587 |
| 3 | SUZLON | 0.2546 |
| 4 | IRFC | 0.2524 |
| 5 | HAL | 0.2515 |

**Context:** A prior composite screen (low Nifty correlation + ER + stress + liquidity)
favored **TRENT, BEL, ITC, CIPLA, HINDUNILVR** — those names rank much lower on
**ER alone** (e.g. TRENT ~#15, ITC ~#186 on ER-only list).

**Live M2 paper:** Still Nifty-only in production config unless stock pipeline is wired.

---

## 2. BHARTIARTL (single-name deep dive)

| Topic | Finding |
| --- | --- |
| ER rank | **#170 / 213** (median ER **0.1954**) |
| 60d rolling corr to Nifty (median) | **~0.46** (recent year of rolling values higher, ~0.55) |
| F&O liquidity (Fyers, near month) | Tier-1; example ATM **1880** CE/PE OI ~2.0M / 1.5M; lot **475** |
| Stress vs Nifty | Mar 2020 window: less drawdown than Nifty; Jun–Sep 2022: outperformed |
| Promoter pledge | **0%** in recent quarterly shareholding filings |

**M2 read:** Liquid and clean pledge; **weak trend persistence (ER)** and **moderate
Nifty linkage** vs the ER-top names.

---

## 3. Worst closed M2 trades (paper, close date 5–21 Oct 2026)

**Scope:** `M2_DIRECTIONAL` lifecycles; net = confirmed fill charges + gross cash flow.

| Rank | Close (IST) | Contract | Net ₹ | Exit |
| --- | --- | --- | --- | --- |
| 1 | 9 Oct | NIFTY 22350 PE (exp 13 Oct) | **−4,766.87** | 35% premium leg stop |
| 2 | 21 Oct | NIFTY 23050 CE (exp 27 Oct) | **−4,662.67** | 35% leg stop |
| 3 | 16 Oct | NIFTY 22950 PE (exp 19 Oct) | **−4,566.59** | 35% leg stop |

Example worst trade (#1): entered 8 Oct 12:45, exited 9 Oct 09:31; entry/exit
**175.95 → 103.65**; 65 contracts; Nifty **+1.01%** on exit session (adverse for long put).

---

## 4. “~₹22,000 loss on 9 Oct” (all modes M2+M3+M4)

**Not supported on close-date realized P&L:**

| Mode | Net on 9 Oct (trades **closed** that day) |
| --- | --- |
| M2 | +3,297.11 |
| M4 | −2,689.29 |
| M3 | 0 |
| **Total** | **+607.82** |

**Misleading if summed:** same-day **fill cash flow** only → **−24,094** (includes
large **entries** not closed that day, e.g. M4 Nov PE opens).

**Closest ~₹22k in window:** **21 Oct 2026** fill-day net **−22,054** (lifecycle
closes that day were **+6,570**).

**Carry on 9 Oct:** Overnight M2 put + M4 put in at open; exited morning; M4 Nov PE
and M3 roll leg opened same day and carried out.

---

## 5. How to reproduce (Oracle)

```bash
cd ~/fno-automated
# M2 closed P&L window — use trade_accounting on position_lifecycle (M2_DIRECTIONAL)
# Universe ER — scripts were ad hoc; re-run from fo_mktlots + yfinance as in session logs
```

Branch with extra M2 analysis scripts (local): `m2-enhancements` (may include unmerged
`scripts/m2_*.py`). Stash on dev machine may hold WIP: `git stash list`.

---

## 6. Related ops (not M2 alpha)

Watchdog restarts during long four-mode scans (protection heartbeat stale when
`protection.ws_enabled: false`) — fixed **PR #55**, merge `a0a6b14`, background
`paper-protection` thread. See `docs/context/FIX_LOG.md` entry **2026-10-22**.
