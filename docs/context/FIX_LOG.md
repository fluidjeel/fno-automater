# Fix Log

**Purpose:** Durable, append-only record of production/paper incidents, root causes,
fixes, and constraints so future LLM sessions and developers do not revert or
override prior findings.

**How to use:**

1. **Append** new dated entries at the **top** of the dated section (below Standing
   rules).
2. **Never delete** entries.
3. If a later fix supersedes an earlier entry, add a **SUPERSEDED** note on the
   old entry referencing the new entry date and topic.
4. Verify file paths and line refs against `main` before citing them; mark
   `(unverified path)` when a symbol cannot be found.

---

## Standing rules (do not revert)

1. **DISCOVERY PAPER is fully autonomous** and should take as many trades as it
   can. Capital and risk caps may only **resize** (soft sizing), never hard-block
   an entry. No SHADOW modes and no human-approval gates in DISCOVERY. Only signal
   and market filters (quote staleness, event blackout, sizing) may reject an
   entry.
2. **Gateway edits** (`src/trading/risk/gateway.py`) for DISCOVERY autonomy are
   allowed only behind a **PAPER + DISCOVERY** guard. STRICT and LIVE behaviour
   must remain unchanged. LIVE stays off.
3. **Never modify** `src/trading/oms/` or `src/trading/broker/`. One slice per PR.
4. **Paper broker fill prices** live in `average_fill_price.value` on FILLED order
   events (see topic B). Do not add a "missing fill price" fix.
5. The **CAS depth collector** (`cas_depth`) is separate from the paper depth
   pipeline; new paper depth code must not import `cas_depth`.
6. **Do not restart** `fno-paper-session.service` mid-session with open positions
   unless the exit-quote fix (DISC-A19) is deployed, because a restart empties
   the paper broker quote cache.
7. **Exactly one Fyers data WebSocket per Fyers account.** `fno-data-tick` owns
   it; other processes (paper protection, DISC-A18 promoted depth, CAS collector
   index feed, capability probe) must consume via the shared hub (**DISC-A22**)
   or use batched REST — never open their own `data_ws` socket. TBT (CAS) socket
   is a separate type with its own limit (3 connections × 5 symbols).

---

## 2026-09-28 (Monday) Discovery paper session

### A. Paper entries frozen and protection monitor not watching held legs

#### What was observed

| Time (IST) | Trade | Structure | Legs / premiums |
| --- | --- | --- | --- |
| 09:56 | TRD-…017 | Straddle | 22950PE @200.65 / 22950CE @176.05 |
| 09:56 | TRD-…045 | Strangle | 22800PE @137.25 / 22900CE @202.75 |
| 10:11:15 | TRD-…025 | — | 22900PE @180.00 / 22900CE @195.05 |
| 10:11:15 | TRD-…053 | — | 22950PE @203.80 / 23000CE @144.90 |

At **10:11:19** `STRATEGY_PNL` stops fired on …017 and …045. Only …017's 22950PE
exit filled (@203.20). …017's 22950CE exit and …045's 22800PE exit were rejected
`PRICE_UNAVAILABLE`; …045's call exit was never sent.

From ~**10:12** entries froze with `RECONCILIATION_UNRESOLVED`, `ENTRY_FROZEN`,
`PROTECTION_DEGRADED` and **3 unresolved CRITICAL** reconciliation events.
Heartbeat showed `ws_connected: true` but `last_quote_at: null`.

#### Root cause

| ID | Cause | Code location (verified on `main`) |
| --- | --- | --- |
| A1 | Paper broker quote cache populated only on the entry path (`_publish_quotes`); exits find no quote → `PRICE_UNAVAILABLE` | `src/trading/runtime/paper_runner.py` — `_publish_quotes` (~L2991), called from entry path (~L2734) |
| A2 | `_quote_is_stale` measures age from bar `event_time` via `SnapshotTimes.age_at`, not quote/calculation time, so quotes read stale from +120 s to +300 s of every 5-minute bar → bar-timer `PROTECTION_DEGRADED` | `paper_runner.py` `_quote_is_stale` (L2284–L2291); `SnapshotTimes.age_at` (L59–L61 in `src/trading/domain/contracts/snapshot.py`) |
| A3 | Protection WS handler never wired pre-A20: `FyersWsQuoteMonitor` did not call the handler and reported connected unconditionally; REST fallback was `lambda: {}` → only the 60 s loop protected positions | `src/trading/runtime/fyers_ws_monitor.py` (`FyersWsQuoteMonitor`, L39+); `src/trading/runtime/protection.py` (`ProtectionCoordinator`, L44+) |
| A4 | `manage_exits` skips a trade when `_exit_leg_snapshots` returns `None` (missing/stale leg snapshot); a leftover leg after partial exit is unmonitored | `paper_runner.py` `manage_exits` (L889+); `_exit_leg_snapshots` (L1787–L1804) |
| A5 | ₹130 structure P&L stop is too tight for multi-leg debit structures (see topic D) | `src/trading/trade/exits.py` `build_exit_policy` / `_determine_strategy_exit` |

#### Fix / status

| Slice | Status | Detail |
| --- | --- | --- |
| **DISC-A19** | In progress (not merged at time of writing) | Publish quotes for exits; DISCOVERY-only staleness from quote time not bar open; missing-leg exits managed; PAPER-only ops command to retry/force-exit stuck exit legs and resolve reconciliation events |
| **DISC-A20** | Merged — PR #31 / commit `acf324a` | `ProtectionCoordinator` wires `ws.set_handler(self._on_quote)` (L86–L87); refreshes heartbeat `last_quote_at` on every quote. `FyersWsQuoteMonitor`: multi-symbol daemon, parses `ltp`/`bid_price`/`ask_price` into `MarketQuote`, `ws_connected` false until first tick, reconnects. `RestQuoteMonitor`: batches held-leg symbols into one `/quotes` call every `rest_poll_seconds` (2 s, ≤30 calls/min) with exponential backoff on 429. `paper_session._protection_rest_fetch` (L1100+) uses dedicated `FyersMarketFeed`. |

**Deployment:** A20 merged but **not yet deployed** to Oracle at time of writing.
A19 + A20 will be deployed together; paper session restarted after both land.

#### Must not be reverted / constraints

- Handler wiring (`ProtectionCoordinator` → `FyersWsQuoteMonitor.set_handler`).
- Connected-only-after-first-tick semantics (`ws_connected` false until first tick).
- Real REST fallback (`_protection_rest_fetch`, not empty lambda).
- After A19 merges: exit-path quote publishing and quote-time staleness for
  DISCOVERY protection.

#### M2 post-entry guidance

M2 does **not** need a depth WebSocket after entry, but a **60 s poll is
insufficient**. Protection-monitor quote streaming on held legs is the correct
design: **WS primary, REST every 2 s, 5 s staleness threshold**.

M2 uses the `long_option` template (`src/trading/strategies/long_option.py`):

| Parameter | Value |
| --- | --- |
| Stop | 40 ticks |
| Target | 80 ticks |
| Trail activation | after 40 ticks |
| Trail distance | 20 ticks |
| Flatten | 15:20 IST (`FLATTEN_IST = (15, 20)`) |

SL/TP are software-enforced. The 10:30 / 14:30 reviews are only the M2 carry
gate.

---

### B. Fill prices were never missing

#### What was observed

A read-only investigation queried fill-price fields and found nulls at the
top level, which looked like missing data.

#### Root cause

Fill prices are stored correctly:

| Location | Field |
| --- | --- |
| FILLED order events in `trading_events` | `average_fill_price.value` |
| Charge inputs | `inputs.premium_per_contract` (`src/trading/domain/contracts/fill_charges.py`) |
| Position legs | `legs[].average_entry_price` (`src/trading/domain/contracts/position.py`) |

Querying a top-level fill-price field returns null. The real problems were the
exit/protection bugs in topic A (quote cache only on entry, bar-open staleness,
unwired protection monitor, missing-leg trades skipped, too-tight P&L stop).

#### Fix / status

**No fix required.** Do not "fix" fill prices.

#### Must not be reverted / constraints

Do not add a "missing fill price" remediation. Investigate quote/protection paths
instead.

---

### C. Monthly-chain change (DISC-A17)

#### What was observed

PR #28 merged as `df37065` after conflicts with PR #29 were resolved; CI green.
Deployed to Oracle together with DISC-A16 (`da2f793`, CAS depth fixes) and PR #29
(`49d6df1`, watchdog loop fix). VM test run: **1,978 tests passed**.

#### Root cause / correction

**IMPORTANT:** The paper session was **NOT** restarted after this deploy. It kept
running the older code (active since **04:40:02 UTC**, `NRestarts=0`) on purpose,
because restarting before the DISC-A19 exit-quote fix would empty the paper broker
quote cache.

A17 takes effect at the **next restart** (planned with A19 + A20).

#### Fix / status

| Slice | Commit | Scope |
| --- | --- | --- |
| DISC-A17 | `df37065` | Monthly-window chain fetch for M3/M4 binders (20–35 DTE) |
| DISC-A16 | `da2f793` | CAS depth dedup, DUPLICATE flag, top-5 imbalance |
| PR #29 | `49d6df1` | Watchdog loop fix |

**Post-restart check:** monthly `NIFTY26OCT*` symbols appear in `trading_events`.

**A16 still needs verification** with a short CAS collector run:

- No repeated ask prices
- DUPLICATE flag present
- Top-5 imbalance non-zero
- `health.json` covers each symbol

No CAS collector runs persistently at time of writing.

#### Must not be reverted / constraints

Do not restart paper mid-session without A19 deployed (standing rule 6).

---

### D. Multi-leg DISCOVERY exits on combined structure P&L

#### What was observed

`STRATEGY_PNL` scope exits fired prematurely on straddle/strangle structures
during the 10:11 incident (topic A).

#### Root cause

Current behaviour (`src/trading/trade/exits.py`):

| Aspect | Current | Problem |
| --- | --- | --- |
| Stop / target | Fixed tick amounts: `pnl_stop` = 40 ticks × 0.05 × qty = **₹130**; target **₹260** (`build_exit_policy` L271–L288; straddle/strangle `STOP_TICKS=40`, `TARGET_TICKS=40` in `src/trading/strategies/m4_broad_basket.py`) | Not scaled to premium paid/received; ₹130 on a ~₹375 straddle is noise and fires on spread alone |
| Marking | `strategy_unrealized_pnl` sums legs at bid (longs) / ask (shorts) (L397–L406) | Stale leg last price still summed; no trailing for structures (`tighten_exit_policy` is LEG_PRICE only, L409+) |
| Auxiliary stop | `_auxiliary_stop_breached` places single-leg stop 40 ticks (₹2) from entry on monitor leg (L214–L234) | Can exit whole structure while in profit |

Scope assignment: multi-leg intents get `ExitScope.STRATEGY_PNL` automatically
(`src/trading/trade/manager.py` L569–L573).

#### Fix / status

**Planned:** DISCOVERY-only slice **DISC-A21** (after A19 merges).

| Change | Detail |
| --- | --- |
| Premium-scaled stops | Debit structures: stop ~35% of debit, target 60–100%. Credit structures: take profit ~50% of credit, stop at 1.5–2× credit, capped at max loss |
| Structure trail | Trail on structure P&L high-water mark, persisted across restarts |
| Auxiliary stop | Drop or keep only as disaster backstop |
| Freshness | Require all legs fresh; REST refresh for stale leg before deciding |
| Trigger | Mid with 2-quote confirmation |
| Exit order | Short legs first |
| STRICT/LIVE | Unchanged |

#### Must not be reverted / constraints

Do not loosen STRICT/LIVE exit policy while implementing DISCOVERY-only DISC-A21.
Do not deploy premium-scaled stops without the quote/protection fixes in A19/A20.

---

### E. DISC-A18 rate-limit-aware depth pipeline (PR #30)

**Branch:** `cursor/disc-a18-depth-promotion-9652`
**Status:** Draft; merge conflicts with `main` after A17. Intentionally held until
A19/A20 are deployed and verified; then rebase, CI, merge, deploy.

> **Note:** Because this PR is not merged, its modules exist **in PR #30** only
> (not on `main` at time of writing).

#### Design (in PR #30)

| Component | Behaviour |
| --- | --- |
| **Wide fetch** | REST options-chain (`options-chain-v3`, 25 strikes) cached 90 s TTL (`paper_chain_cache.py`); on Fyers 429 exponential backoff (base 30 s, cap 300 s, jitter 0.10) serves cached chain |
| **Promotion** (`depth_promotion.py`) | Strikes scored 0.50 OI + 0.30 volume + 0.20 ATM proximity; depth set size 8 (allowed 5–10); promote after 2 consecutive polls in top set, demote after 3 outside |
| **Depth feed** (`promoted_depth_ws.py`) | Promoted strikes only: 5-level depth via `data_ws` channel 11 `DepthUpdate`; incremental subscribe/unsubscribe |
| **Attachment** (`paper_option_depth.py`) | WS book sizes; REST depth fallback when socket down and no backoff active |
| **Wiring** (`paper_market_stack.py`) | Integrates chain cache, promotion, and depth feed |
| **Removed duplicates** | Per-poll `fetch_quotes` for near-strike candidates; REST depth for top-OI symbols |
| **Health** | Heartbeat JSON gains `chain_cache`, `depth_promotion`, `depth_attachment` |
| **Enums** | `ChainFetchMode` (LIVE, CACHED, BACKOFF); `DepthFeedSource` (WEBSOCKET, REST_FALLBACK, UNAVAILABLE) |
| **Config** | `config/paper_data.yaml` → `depth_promotion` |
| **Tests** | 8 new tests; full suite, ruff, mypy passed in PR |

#### Open risks

- Fyers per-connection symbol limits not encoded
- SDK `unsubscribe` support assumed
- REST depth suppressed during backoff → promoted strikes may briefly have no depth
- Following-week chain still an extra cached fetch
- Candidate quotes from chain up to 90 s old; during backoff can approach/exceed
  120 s P0 freshness limit
- Likely conflicts with A20 in `paper_session.py`

#### Must not be reverted / constraints

Hold merge until A19/A20 deployed and verified on Oracle.

---

### F. Other observations (not yet fixed)

#### CAS depth benchmark (09:44–10:14 IST)

| Metric | Value |
| --- | --- |
| Messages | 4,513 (2.5 updates/s) |
| Dropped | 0; max queue 1 |
| Latency avg / p99 | 0.15 ms / 0.23 ms |
| Reconnects / seq gaps | 0 / 0 |
| Duplicates / stale | 2 / 2 |
| Symbols with data | 2 of 5 (NIFTY26SEP22900PE 3,578; RELIANCE26SEP1300CE 935) |
| NIFTY50-INDEX TBT | Sent nothing |
| MCX GOLDM/CRUDEOILM | Rejected (-300 invalid symbol): `_resolve_mcx_symbol` in `src/trading/data/cas_depth/collector.py` (L376–L389) only tries 2025 `25OCTFUT`/`25SEPFUT` contracts |
| `mcx_supported: True` | Misleading — set whenever any message arrives (`collector.py` L154–L159, L328) |
| Liquid-strike picker | Chose thin RELIANCE contract (3 bid vs ~44 ask levels); prefer near-the-money with 2–14 DTE |
| Memory | 82.5 MB (single sample) |

#### Calendar

Code assumes Thursday expiries but NSE weekly expiries are Tuesdays
(`src/trading/identification/calendar.py`). `min_dte_new_entry` in
`src/trading/config/discovery.py` is unused.

#### Data pipeline errors

Fyers 429 at 09:38; `FyersApiError` at 09:54 and 09:55.

Failed units not yet investigated: `fno-agent-advise`, `fno-agent-weekly`,
`fno-paper-post-open-check`.

#### Pending audit

DISCOVERY reject reasons to review — remove any that are not signal/market filters
for DISCOVERY:

`INSTRUMENT_UNKNOWN`, `PRICE_UNAVAILABLE`, `DATA_FEED_ERROR`,
`DATA_STALE`/`DATA_INVALID`, `DEPTH_INSUFFICIENT`, `ONE_LOT_OVER_GUIDE`

---

### G. Fyers limits and single data-socket decision

#### What was observed

Fyers API rate limiting is per account, not per app. We already hit one **429 at
09:38 IST** on 2026-09-28. Multiple processes on the same account were each
opening or planning their own `data_ws` connection.

#### Fyers limits (documented)

| Category | Limit |
| --- | --- |
| REST (general) | 10/s, 200/min, 100k/day |
| Order place + modify + cancel | 10/s |
| `/data/quotes` | max 50 symbols per call |
| `/data/depth` | 1 symbol, 5 levels |
| Option chain | max 50 strikes |
| Data socket (`data_ws`) | 5,000 symbols per connection |
| TBT socket | 3 connections × 5 symbols; NFO + NSE equity only |

Exceeding the per-minute cap **more than 3 times in a day** blocks API access
for the rest of the day.

Multiple apps per account are allowed, but Fyers support confirmed rate limiting
is **"not based on APP, its overall"** (per account).

#### WebSocket policy

| Source | Statement |
| --- | --- |
| Fyers staff (Aug 2024) | "one connection per API key" |
| Fyers staff (Apr 2025) | "at once you will be able to connect to a single websocket" |
| Per-app separation | Not documented |

**Adopted rule:** one `data_ws` socket per account (conservative). REST
per-account pooling is **confirmed by support**; the single-socket rule is a
**conservative assumption** pending clearer per-app documentation.

**Sources:** [FyersDev/fyers-skills](https://github.com/FyersDev/fyers-skills)
(`rate-limits.md`, `websocket.md`, `market-data.md); Fyers community threads
["Multiple apps using same client id"](https://fyers.in/community)
and "Websocket Queries".

#### Audit (2026-09-28)

| Process | Data socket | Status |
| --- | --- | --- |
| `fno-data-tick` | Holds one `data_ws` all session | Active owner |
| Paper protection (`FyersWsQuoteMonitor`, DISC-A20) | Would open a **second** `data_ws` in the paper session | **Interim REST-only:** `protection.ws_enabled: false`, `rest_poll_seconds: 3` until DISC-A22 lands |
| DISC-A18 promoted depth (`promoted_depth_ws.py`, PR #30) | Would add a **third** `data_ws` | Must rebase onto DISC-A22 hub before going live |

#### Fix / status

**Planned:** **DISC-A22** — shared Fyers data WebSocket hub; `fno-data-tick`
remains owner; paper protection, DISC-A18 depth, CAS index feed, and capability
probe consume via hub or batched REST.

#### Must not be reverted / constraints

- Do not enable `protection.ws_enabled: true` or merge DISC-A18 depth WS until
  DISC-A22 hub is deployed.
- Do not open additional `data_ws` connections from paper session, CAS collector,
  or capability probe.
- TBT sockets remain separate (CAS depth); respect 3 × 5 symbol limit.
