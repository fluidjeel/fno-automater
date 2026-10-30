# Live tick stream — specification

Follow [INDEX.md writing rules](./INDEX.md).

**Ticks** = single price updates from the exchange, streamed over a **websocket** (one long-lived connection to Fyers).

Code folder: `src/trading/data/tick_consumer/`

---

## ws_multiplex (one websocket for all symbols)

**Job:** Keep exactly **one** Fyers websocket. Subscribe to Nifty index first; add more symbols later without opening a second connection.

| | |
| --- | --- |
| **Reads** | Fyers login settings, token cache, symbol list (v1: `NSE:NIFTY50-INDEX` only) |
| **Produces** | A stream of `TickEvent` records (see table below) |
| **How often** | Every time the broker sends a tick |
| **If disconnected** | Reconnect with backoff from existing `data_pipeline.yaml`; emit health event `connected=false` |

### `TickEvent` (standard shape)

| Field | Type | Required |
| --- | --- | --- |
| `schema_version` | string | `"1"` |
| `symbol` | string | yes |
| `ltp` | decimal string | **last traded price** |
| `volume` | int | yes if broker sends it, else `0` |
| `trade_size` | int | size of last trade; `0` if unknown |
| `exchange_ts` | datetime | broker time if present, else `received_at` |
| `received_at` | datetime | when we received it |
| `source` | string | `"fyers_ws"` |

**Done when:** During 10:00–15:00 IST on a busy day, average at least one tick per second; never run a second websocket process.

---

## signing (guess buy vs sell on each tick)

**Job:** Label each tick as buyer- or seller-initiated using the latest **order book** (top five bid/ask prices).

| | |
| --- | --- |
| **Reads** | `TickEvent`, latest `DepthSnapshot` from minute backbone |
| **Produces** | `SignedTick` = tick plus `sign` and `sign_confidence` |

### `sign` values

`BUY | SELL | UNKNOWN`

### Rules (fixed logic)

1. If order book is older than allowed → `UNKNOWN`, confidence `0`.
2. If last price ≥ best ask → `BUY`.
3. If last price ≤ best bid → `SELL`.
4. If last price above mid → `BUY`, confidence `0.5`.
5. If last price below mid → `SELL`, confidence `0.5`.
6. Else → `UNKNOWN`.

**Mid** = average of best bid and best ask.

**Done when:** Unit tests cover rules 2–6 with a fixed fake order book.

---

## bars_1m (one-minute candles from ticks)

**Job:** Roll ticks into **one-minute bars** aligned to the clock in IST.

| | |
| --- | --- |
| **Reads** | `SignedTick` stream |
| **Produces** | `Bar1m` at each minute boundary |
| **How often** | One bar per symbol per minute |

### `Bar1m` fields

| Field | Type |
| --- | --- |
| `schema_version` | `"1"` |
| `symbol` | string |
| `bar_start_ist` | datetime (minute start) |
| `open`, `high`, `low`, `close` | decimal string |
| `volume` | int (sum of trade sizes) |
| `buy_volume` | int (ticks signed BUY) |
| `sell_volume` | int (ticks signed SELL) |
| `unknown_volume` | int |
| `tick_count` | int |

Do **not** fetch historical bars from REST for this pipeline.

**Done when:** Bars start at `:00` seconds IST; first session bar starts 09:15.

---

## delta (running buy-minus-sell volume)

**Job:** Track session and five-minute **delta** = buy volume minus sell volume.

| | |
| --- | --- |
| **Reads** | `SignedTick` |
| **Produces** | `DeltaState` in memory; snapshot each minute |

### `DeltaState`

| Field | Type | Meaning |
| --- | --- | --- |
| `session_date` | date | trading day |
| `session_delta` | int | buy − sell since 09:15 |
| `session_buy_volume` | int | session |
| `session_sell_volume` | int | session |
| `delta_5m` | int | rolling last 5 minutes |
| `cumulative_delta` | int | same as `session_delta` in v1 |

On each signed tick, add `trade_size` to buy or sell totals.

**Done when:** A test replay matches hand-calculated delta.

---

## store (save ticks and bars to disk)

| | |
| --- | --- |
| **Reads** | ticks and bars |
| **Writes** | parquet files under paths in [00-global.md](./00-global.md) |
| **Disk error** | Log critical; keep daemon running; platter may mark tick freshness bad (does **not** block paper entries by itself) |

**Done when:** Same-day feature job can read the files; schema stays stable.
