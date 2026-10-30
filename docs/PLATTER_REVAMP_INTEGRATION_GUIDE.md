# Platter revamp — how everything fits together

Plain-language guide to the paper-trading revamp. Canonical detail lives in
`docs/specs/revamp/`; this document explains **end-to-end flow**, **timing**,
**failures**, and **Monday’s first session** on the Oracle server.

---

## Terms (used once)

| Term | Meaning |
| --- | --- |
| **Fyers** | Broker API for live prices, option chain, and depth. |
| **IST** | India Standard Time (UTC+5:30). Market hours are in IST. |
| **Tick** | One price update from the exchange. |
| **Websocket** | One long-lived connection that streams ticks. |
| **Minute bundle** | One JSON snapshot per clock minute: spot quote, option chain, depth. |
| **Feature row** | Numbers derived from ticks, bars, and the minute bundle (flow, structure, etc.). |
| **Platter** | One JSON “decision packet” every five minutes for the agent. |
| **Agent** | Code (optional LLM help) that picks LONG, SHORT, or NEUTRAL from the platter. |
| **Gate** | Maps agent output to APPROVE or VETO for **new** entries. |
| **Verdict file** | Small JSON the paper session reads before opening a trade. |
| **Paper session** | Long-running process that simulates trades and talks to Telegram. |
| **Exit engine** | Separate rules that close open trades (stop, target, time). |
| **Telemetry** | Append-only logs of each agent decision for later review. |
| **Shadow backtest** | Offline replay of the rubric on saved platters; optional live shadow compares old vs new gate. |

---

## Big picture

Market data moves **in parallel** on two paths:

1. **Fast path (ticks)** — websocket → signed ticks → one-minute bars → parquet files.
2. **Slow path (REST, once per minute)** — option chain + depth + spot quotes → minute bundle → feature row.

Every **five minutes**, the **agent** builds a **platter**, scores it, writes a **verdict**, logs **telemetry**, and (if paper session is running) the **entry gate** may allow a new simulated trade. **Exits** ignore the agent and run on their own schedule from open prices.

All pipeline file writes use **atomic saves** (lock + temp file + rename) so two jobs never corrupt the same file.

---

## Text diagram (data flow)

```
                    Fyers account (one websocket + REST limits)
                                    |
          +-------------------------+-------------------------+
          |                                                   |
    [Websocket ticks]                              [REST each minute]
          |                                                   |
   tick consumer                                      minute backbone
   (sign, delta, bars)                               (chain + depth + quotes)
          |                                                   |
          v                                                   v
 data/platter/ticks/*.parquet              data/platter/minute/minute_HHMM.json
 data/platter/bars_1m/*.parquet                         |
          |                                             v
          +-----------------------> features compute ---+
          |                             |
          |                             v
          |              data/platter/features/features_HHMM.json
          |                             |
          |         every 5 min (09:15..15:30 IST)     |
          |                             v
          |                    platter assembly
          |                             |
          |                             v
          |              data/platter/platters/.../platter_*.json
          |                             |
          |                             v
          |                    platter agent (+ optional LLM)
          |                      /            \
          |                     v              v
          |     data/paper/m2_veto_verdict.json   telemetry/decisions.jsonl
          |                     |
          |                     v
          |            paper session (if enabled)
          |              admit_m2_entry → simulated order
          |                     |
          +---------------------+--------> exit engine (stops/targets/time)
                                        |
                                        v
                              Telegram (fills, EOD, alerts)
```

**Depth hint (cross-link):** After each minute bundle is saved, a small hint file
updates top bid/ask so the tick consumer can **sign** ticks (buy vs sell) without
extra REST depth calls on the websocket path.

---

## Stage by stage

### 1. Tick consumer (`trading platter tick-daemon`)

**Job:** Keep **one** Fyers websocket on Nifty index (and more symbols later without a second connection).

**Timing:** Continuous during the session; each broker message is handled immediately.

**Writes:**

- Raw signed ticks → `data/platter/ticks/{session_date}/ticks.parquet` (batched flush).
- Completed one-minute bars → `data/platter/bars_1m/{session_date}/bars.parquet`.
- Delta state (cumulative buy/sell pressure) → JSON state file for the features job.

**Reads:** Latest depth hint from the minute backbone (bid, ask, timestamp).

**On failure:**

- Disconnect → reconnect with backoff; health JSON records `connected=false`.
- Missing depth hint → ticks still store; signing may be `UNKNOWN` (features mark quality, paper does not block entries for that alone).
- Flush uses locked atomic parquet append so the daemon and timers never interleave a half-written file.

**Oracle note:** `deploy/install.sh` still starts `fno-data-tick.service` with `trading data stream --daemon` (legacy stream). Full revamp wiring uses **`trading platter tick-daemon`** for the platter tick store paths above. For Monday, run **one** tick process only—either migrate the systemd unit to `platter tick-daemon` or accept that legacy ticks feed the old pipeline while platter REST path still runs.

---

### 2. Minute backbone (`trading platter minute`)

**Job:** Once per clock minute, call Fyers REST for option chain, depth, and index quotes; merge into one **minute bundle**.

**Timing:** **Every 60 seconds** from about **08:30–15:30 IST** (systemd timer uses UTC; see Oracle timers below).

**Writes:** `data/platter/minute/{session_date}/minute_{HHMM}.json` (atomic JSON).

**On failure:**

- HTTP **429** (rate limit) → bundle status **DEGRADED**; reuse previous minute’s chain or depth if available instead of failing silent.
- If chain **and** depth cannot be obtained even with fallback → **no file** for that minute; downstream features skip until a bundle exists.
- After save → **publish depth hint** for the tick consumer.

**Chain to features:** Orchestration module `run_minute_and_features` runs minute backbone **then** features in one step so a successful minute always produces a matching feature row. The systemd service should call that combined step (or run `features` immediately after `minute` in the same unit).

---

### 3. Features (`trading platter features` or chained after minute)

**Job:** Combine minute bundle, tick bars, delta state, and config into one **feature row** JSON.

**Timing:** Same minute as the bundle (within seconds after the minute job).

**Writes:** `data/platter/features/{session_date}/features_{HHMM}.json` (atomic JSON).

**On failure:**

- Missing minute file → features command exits without write.
- Missing ticks → many fields get quality flag **MISSING**; paper mode still builds the row.
- Stale optional feeds → flags only; they do **not** set platter `fail_closed`.

---

### 4. Platter assembly (inside agent cycle)

**Job:** At each **five-minute mark**, pick the latest feature row **at or before** that mark, load the matching **minute bundle** for the real Nifty **last price** and timestamps, and merge into one **DecisionPlatter** JSON.

**Timing:** **76 times per session** at `09:15, 09:20, …, 15:30` IST.

**Writes:** `data/platter/platters/{session_date}/platter_{HHMMSS}.json`.

**Freshness on platter:**

- **`fail_closed`** is driven mainly by **spot quote age** (default: older than **60 seconds** → block new entries).
- Spot price and `as_of_utc` come from the **minute bundle Nifty quote**, not from unrelated structure fields.
- Chain, depth, tick, and bar ages are recorded for weekly review but do **not** block entries in paper mode.

**On failure:**

- No feature row for the mark → agent writes **VETO**, telemetry `missing_platter=true`, no platter file.

---

### 5. Platter agent (`trading platter agent`)

**Job:** Score the platter (rubric; optional LLM overlay), choose LONG / SHORT / NEUTRAL, map through the **gate**, write **verdict** and **telemetry**.

**Timing:** Same five-minute grid as assembly; timer starts at **09:15 IST** (first mark).

**Writes:**

- `data/paper/m2_veto_verdict.json` — what the paper entry path reads.
- `data/paper/m2_veto_log.jsonl` — history line per decision.
- `data/telemetry/agent_decisions/{session_date}/decisions.jsonl` — full context for analytics.

**Paper gate rules (`platter_gate`):**

- **VETO** if platter missing, `fail_closed` (stale spot), or **event blackout** active.
- Otherwise **APPROVE** at full size (`free_trade` paper mode). Agent vs mechanical disagreement is logged only, not blocked.

**On failure:**

- LLM unreachable or bad JSON → rubric-only direction; reason stored in telemetry.
- Stale spot → skip LLM, NEUTRAL direction, gate still **VETO** via `fail_closed`.

---

### 6. Gate and paper execution

**Gate** does not send orders. The **paper session** (when enabled) loops on market data, builds strategy signals, and calls **`admit_m2_entry`**, which reads the **verdict file** only.

**Timing:** Paper loop runs continuously in session; entries are attempted when strategy logic fires, not necessarily on the five-minute boundary. The verdict is refreshed every five minutes and expires after `valid_for_seconds` from config.

**On failure:**

- **VETO** → no new entry that cycle; Telegram may say “No new trade this cycle (blocked by rules).”
- **APPROVE** → normal sizing path (paper broker).

**Oracle note:** For a clean platter-only Monday, old timers (`fno-paper-session`, legacy `m2-veto`) may be **disabled** while platter agent timers stay on. Then verdicts and telemetry update, but **no simulated trades or trade Telegram lines** until paper session is re-enabled.

---

### 7. Exit engine (unchanged by revamp)

**Job:** Close or reduce open legs using stop loss, profit target, time exit, and trailing rules.

**Timing:** Evaluated on **each price tick** (or each paper cycle price update), not on the five-minute grid.

**Isolation:** Does **not** read platter JSON, agent direction, or verdict file. Deleting the verdict does not change exits.

**On failure:**

- Missing price → no exit signal (`PRICE_UNAVAILABLE`); no panic flatten unless existing degraded rules say so.

---

### 8. Telemetry

**Job:** Append one **AgentDecisionRecord** per agent run (including VETO without LLM).

**Timing:** Immediately after each agent cycle.

**Maintenance timers:**

- **Daily retention** — trim old telemetry so disk does not fill (02:00 IST / 20:30 UTC).
- **Weekly rollup** — Saturday summary JSON for review (16:00 IST Saturday / 10:30 UTC).

**On failure:** Disk full or permission errors surface in systemd journal; agent still attempts verdict write separately.

---

### 9. Shadow backtest and live shadow

**Shadow backtest (`trading platter shadow-backtest`):** Offline. Replays saved platters through the rubric for research. Does not touch live verdict or broker.

**Live shadow (optional config):** Runs legacy `m2_veto_cycle` beside platter agent and logs disagreements to `data/paper/platter_shadow.jsonl`. Only one **`gate_authority`** actually controls entries (`legacy` vs `platter`).

---

### 10. Telegram notifications

**Auth:** Paper session can drive Telegram login for Fyers when needed.

**Trade messages:** Plain-language lines on entry/exit fills (`format_post_trade`).

**End of day:** Session summary when trades closed or none (`format_eod_report`).

**Alerts:** Data feed failures and drawdown alerts from the paper runtime (separate from platter telemetry).

Platter agent itself does **not** send Telegram; execution layer does after orders.

---

## Oracle deploy timers (systemd)

Times below are **Monday–Friday** unless noted. UTC is what systemd uses; IST is market local time.

| Timer / service | Schedule (UTC) | Rough IST | Command |
| --- | --- | --- | --- |
| `fno-data-tick.service` | Always on (restart) | Session hours | `trading data stream --daemon` (legacy; see tick note) |
| `fyers-refresh.timer` | Per deploy file | Before session | Refreshes Fyers token |
| `fno-platter-minute.timer` | Every minute **03:00–10:00** UTC | **08:30–15:30** | `trading platter minute` |
| `fno-platter-agent.timer` | Every **5 min** from **03:45** UTC | **09:15–15:30** | `trading platter agent` |
| `fno-telemetry-retention.timer` | Daily **20:30** UTC | **02:00** next day | `trading platter telemetry retention` |
| `fno-telemetry-weekly.timer` | Sat **10:30** UTC | **16:00** Sat | `trading platter telemetry weekly` |

Install path: `deploy/install.sh` on the server copies units into `/etc/systemd/system/` and enables platter + telemetry timers.

---

## Timing summary

| Layer | Cadence | IST anchor |
| --- | --- | --- |
| Ticks | Sub-second to few per second | 09:15–15:30 session |
| Minute REST + features | **60 s** | From ~08:30 prep through 15:30 |
| Platter + agent + gate | **5 min** | 09:15, 09:20, …, 15:30 (76 marks) |
| Verdict validity | Config (`valid_for_seconds`) | Refreshed each agent run |
| Exits | Per tick / cycle | Whole session while open |
| Telemetry retention | Daily | Off-hours |
| Weekly rollup | Weekly | Saturday afternoon |

---

## Failure summary (what blocks new trades in paper)

| Stage | Symptom | New entries? |
| --- | --- | --- |
| Tick daemon down | Stale tick/bar files on platter | **No** only if spot quote stale on platter; optional feeds degraded |
| Minute job fails | No minute/features for that minute | Agent may VETO at next mark if no feature row |
| Features skip | Gap in `features_*.json` | Agent uses last row ≤ mark; missing row → VETO |
| Stale Nifty quote on platter | `fail_closed=true` | **VETO** |
| Event blackout | `events.blackout_active` | **VETO** |
| Agent crash | Old verdict expires | **VETO** when expired or missing |
| Paper session off | Verdict updates only | No trades regardless of APPROVE |

Exits and protection for **already open** positions continue under deterministic rules even when entries are blocked.

---

## Monday’s first session (end to end)

Assume Oracle repo at `~/fno-automated`, `.env` with Fyers and Telegram, platter timers **enabled**, paper session **disabled** (telemetry-only Monday) or **enabled** (full simulation).

**Before 09:15 IST**

1. **Fyers token** fresh (`fyers-refresh` or manual).
2. **Tick process** running (legacy `data stream` or revamp `platter tick-daemon`—pick one).
3. **Minute timer** already firing each minute; each successful run should produce **minute JSON + feature JSON** for that clock minute.
4. Depth hints update after each minute so tick signing stays aligned.

**09:15 IST — first agent mark**

1. Agent loads latest feature row ≤ 09:15 and minute bundle for spot quote.
2. Builds platter, runs rubric (+ LLM if configured).
3. Gate writes APPROVE or VETO to `m2_veto_verdict.json`.
4. Appends one line to `decisions.jsonl`.

**09:15–15:30 IST**

- Every minute: REST backbone + features (parallel to ticks).
- Every five minutes: platter + agent + verdict + telemetry.
- If paper session **on**: strategy cycles read verdict; on APPROVE and mechanical setup, simulated entry → Telegram fill lines; exit engine manages open trades.
- If paper session **off**: steps 1–4 still run; no orders; no trade Telegram (auth/EOD scripts may still run if scheduled).

**After 15:30 IST**

- Agent timer stops adding marks after the 15:30 grid point; verdict should not be overwritten post-close (same rule as legacy five-minute veto).
- Open positions (if paper was on) still exit on tick rules until flat.
- EOD Telegram when session wrapper sends summary.
- Overnight: telemetry retention may prune old files.

**First-week review**

- Weekly telemetry rollup Saturday.
- Optional `platter shadow-backtest` on stored platters under `data/platter/platters/`.
- Compare agent direction vs mechanical setup in telemetry—not used to block paper entries in v1.

---

## Cohesion checklist (what “wired” means)

1. **One websocket** for platter ticks; depth hint from minute backbone for signing.
2. **Minute job → features job** in one logical step after each REST minute.
3. **Feature row time ≤ platter mark**; minute bundle supplies **real spot LTP** for `fail_closed`.
4. **Agent → verdict → admit_m2_entry** (when paper runs) → broker paper → Telegram.
5. **Exits and agent** stay independent.
6. **Telemetry** captures every agent outcome for later shadow and human review.
7. **Atomic writes** on JSON and parquet platter artifacts under concurrent timers and daemons.

---

## Related docs

- Spec index: `docs/specs/revamp/INDEX.md`
- Shared paper rules: `docs/specs/revamp/00-global.md`
- Operations (recovery, readiness): `docs/context/OPERATIONS_RUNBOOK.md`
