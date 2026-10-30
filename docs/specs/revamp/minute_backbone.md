# Once-per-minute market snapshot — specification

Follow [INDEX.md writing rules](./INDEX.md).

**Minute backbone** = a job that runs every calendar minute during the session and saves option chain + order book + latest quotes into one JSON file.

Code folder: `src/trading/data/minute_backbone/`

---

## scheduler

| | |
| --- | --- |
| **When** | Every 60 seconds, Mon–Fri, 09:00–15:35 IST |
| **Steps** | (1) option chain (2) order book depth (3) merge (4) save file |
| **REST calls** | About 2–3 per minute: chain, depth, optional VIX quote if not on websocket |

If Fyers returns **429** (too many requests): mark bundle `DEGRADED`, keep previous snapshot values.

---

## chain_snapshot (option prices and open interest)

**Reads:** Fyers option chain for nearest liquid expiry (same rules as today’s binder).

**Writes:** `ChainSnapshot`

| Field | Type |
| --- | --- |
| `schema_version` | `"1"` |
| `snapshot_id` | string (hash) |
| `symbol` | string |
| `expiry_date` | date |
| `as_of_utc` | datetime |
| `strikes` | list of `ChainStrikeRow` |

### `ChainStrikeRow`

| Field | Type |
| --- | --- |
| `strike` | decimal string |
| `option_type` | `CALL` or `PUT` |
| `ltp`, `bid`, `ask` | decimal string |
| `oi` | open interest (int) |
| `volume` | int |
| `iv` | implied volatility |
| `delta`, `gamma`, `vega`, `theta` | decimal string (**Greeks** = sensitivity measures) |
| `symbol` | contract code |

**Strikes:** at-the-money ±25 each side (50 lines max per side type).

**Done when:** At most 100 rows; Greeks present when config says `chain_greeks: true`.

---

## depth_snapshot (order book)

**Reads:** `fetch_depth(NSE:NIFTY50-INDEX)`

**Writes:** `DepthSnapshot` with five bid and five ask levels plus spread in basis points.

**Done when:** Five levels filled or quality marked `DEGRADED`.

---

## merge (one bundle per minute)

**Reads:** Latest chain, depth, websocket quote cache, optional REST VIX.

**Writes:** `MinuteBundle` saved to `data/m2_agentic/minute/{session_date}/minute_{HHMM}.json`

| Field | Type |
| --- | --- |
| `schema_version` | `"1"` |
| `minute_start_ist` | datetime |
| `status` | `OK`, `DEGRADED`, or `FAILED` |
| `nifty_quote` | last price, bid, ask, time, source ws or rest |
| `vix_quote` | same shape |
| `chain_snapshot_id` | string |
| `depth_snapshot_id` | string |
| `chain`, `depth` | embedded JSON or path to file |

**Done when:** One file per minute during session; when status is OK, chain age at merge ≤ 120 seconds.
