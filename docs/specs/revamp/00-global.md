# 00 — Shared rules for the whole revamp

Follow the [writing rules in INDEX.md](./INDEX.md).

## Paper mode: learn first, block little

We run in **paper mode** (no real money). The agent’s long / short / neutral labels should **not** heavily block trades. We record disagreements and refine rules later from saved history.

| Topic | Paper rule (v1) |
| --- | --- |
| Agent vs mechanical direction | **Do not block** on mismatch. Log it in telemetry only. |
| “Neutral” agent call | **Do not** cut position size in paper. Full size if mechanical wants in and rails pass. |
| Missing optional data (flow, gamma, etc.) | **Do not block.** Mark quality flags for analytics. |
| Score thresholds | **Soft hints** for the agent, not hard law. |
| Hard blocks (only two) | (1) Spot quote too old. (2) Event blackout active. |

Stricter live-money blocking will be a **future spec version**, not v1.

---

## Session clock (India Standard Time, IST)

**IST** = Indian market local time (UTC+5:30).

| Item | Value |
| --- | --- |
| Timezone name in code | `Asia/Kolkata` |
| Regular session open | `09:15:00` IST |
| Regular session close | `15:30:00` IST |
| `session_date` | Calendar date `YYYY-MM-DD` in IST for trades between open and close |

**Before open (09:00–09:14):** background jobs may run; entries are not expected.

**After close (after 15:30):** agent does not overwrite the verdict file (same as today’s five-minute veto).

---

## Five-minute decision times (IST)

M2 agentic build and agent run at:

`09:15, 09:20, 09:25, …, 15:25, 15:30` — **76 times** per session.

---

## When data is “too old” (hard block)

The decision packet records freshness for many feeds. **Only one freshness rule blocks new trades in paper:**

| Check | Limit | If failed |
| --- | --- | --- |
| Nifty spot **quote age** | > 60 seconds (`quote_max_age_ms` in existing config) | `fail_closed = true` → gate **VETO** (block entry) |

Chain, depth, tick, or bar staleness **does not** set `fail_closed` in paper. Those fields are still stored for weekly review.

Missing decision packet at agent run → **VETO**.

---

## Event blackout (second hard block)

If `events.blackout_active` is true on the decision packet → **VETO** new entries, regardless of agent direction. Log `event_blackout_veto` in telemetry.

---

## Fyers broker limits (must not break)

**Fyers** = our market data broker API.

| Rule | Limit |
| --- | --- |
| Live price **websockets** (persistent connections) | **1** per account |
| Option chain REST calls | **≤ 1 per minute** per underlying |
| Depth REST calls | **≤ 1 per minute** per symbol |
| Strikes each side of ATM | **25** (50 option lines total) |
| HTTP 429 (rate limit) | Back off; mark minute file `DEGRADED`, do not silently skip |

---

## Symbols (v1)

| Role | Fyers symbol string |
| --- | --- |
| Nifty index spot | `NSE:NIFTY50-INDEX` |
| India VIX index | `NSE:INDIAVIX-INDEX` |
| Options chain | Same underlying as today’s pipeline config |

---

## Where files are stored (under repo root)

| What | Path pattern |
| --- | --- |
| Raw ticks | `data/m2_agentic/ticks/{session_date}/ticks.parquet` |
| One-minute bars | `data/m2_agentic/bars_1m/{session_date}/bars.parquet` |
| Once-per-minute bundle | `data/m2_agentic/minute/{session_date}/minute_{HHMM}.json` |
| Computed features | `data/m2_agentic/features/{session_date}/features_{HHMM}.json` |
| Five-minute decision packet JSON | `data/m2_agentic/decision_packets/{session_date}/m2_agentic_{HHMMSS}.json` |
| Live gate verdict (small file) | `data/paper/m2_veto_verdict.json` |
| Gate history line log | `data/paper/m2_veto_log.jsonl` |
| **Decision telemetry** | `data/telemetry/agent_decisions/{session_date}/decisions.jsonl` |
| Weekly rollup | `data/telemetry/weekly/agent_week_{YYYY-Www}.json` |

**Parquet** = columnar file format for large tick history.

---

## Numbers and times in JSON

- Prices and money: decimal **strings** (example `"123.45"`).
- Timestamps: ISO-8601 with timezone; field names often end in `_at` or `_utc`.

---

## Quality flag (on many sub-objects)

`OK | MISSING | STALE | DEGRADED | INVALID` — informational in paper except where tied to hard blocks above.
