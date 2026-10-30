# Computed signals (features) — specification

Follow [INDEX.md writing rules](./INDEX.md).

**Features** = numbers derived from minute bundles, ticks, and bars (VWAP, gamma exposure, order flow, etc.). They are **not** trade gates in paper mode — only stored for the agent and weekly review.

Code folder: `src/trading/data/features/`

**When:** Up to 30 seconds after each minute bundle is saved.

**Reads:** Minute bundle, tick store, one-minute bars, delta state, previous minute’s chain file, prior day levels, `config/platter_pipeline.yaml`.

**Writes:** `FeatureRow` at `data/platter/features/{session_date}/features_{HHMM}.json`.

**Quality flag** on each block: `OK | MISSING | STALE | DEGRADED | INVALID` — tells analytics if data was weak; does **not** block paper entries except via spot quote rule in [00-global.md](./00-global.md).

---

## absorption (`features/absorption.py`)

| Output field | Type | Scope | Owner |
| --- | --- | --- | --- |
| `absorption.level_price` | decimal string | session | absorption |
| `absorption.aggressive_volume` | int | session | absorption |
| `absorption.price_progress_ticks` | int | session | absorption |
| `absorption.flag` | bool | session | absorption |
| `absorption.quality` | QualityFlag | session | absorption |

**Rule:** At rolling level (last 5m high/low), if aggressive volume ≥ config threshold and price progress ≤ config max ticks → `flag=true`.

---

## volume_profile (`features/volume_profile.py`)

**POC / VAH / VAL** = point of control and value area high/low from a volume-at-price histogram of session one-minute bars.
| Field | Type | Scope |
| --- | --- | --- |
| `volume_profile.poc` | decimal string | session |
| `volume_profile.vah` | decimal string | session |
| `volume_profile.val` | decimal string | session |
| `volume_profile.quality` | QualityFlag | session |

Built from session 1m bars volume-at-price histogram (bucket size from config).

---

## vwap (`features/vwap.py`)

**VWAP** = volume-weighted average price (fair “average trade price” for the session or longer windows).
| Field | Type | Scope |
| --- | --- | --- |
| `vwap.session` | decimal string | session |
| `vwap.week` | decimal string | multi-day |
| `vwap.month` | decimal string | multi-day |
| `vwap.dist_session_pct` | decimal string | session |
| `vwap.dist_week_pct` | decimal string | multi-day |
| `vwap.dist_month_pct` | decimal string | multi-day |
| `vwap.quality` | QualityFlag | session |

Week/month anchors: cumulative typical price × volume from stored prior sessions + today.

---

## options_positioning (`features/options_positioning.py`)

| Field | Type | Scope |
| --- | --- | --- |
| `options.max_oi_call_strike` | decimal string | session |
| `options.max_oi_put_strike` | decimal string | session |
| `options.top_delta_oi_strikes` | array of `{strike, option_type, delta_oi}` | session |
| `options.gex_net` | decimal string | session |
| `options.delta_notional_net` | decimal string | session |
| `options.atm_iv` | decimal string | session |
| `options.skew_25d` | decimal string | session |
| `options.oi_change_quality` | QualityFlag | session |

**GEX** (**gamma exposure**) v1 — config version `gex_formula_version: "v1-dealer-short-gamma"`:  
Per strike: `gex_strike = gamma * oi * spot * lot_size`; calls add, puts subtract. Sum over strikes in the snapshot.

**ΔOI:** `oi_now - oi_prev_minute` from previous minute chain file.

---

## session_structure (`features/session_structure.py`)

| Field | Type | Scope |
| --- | --- | --- |
| `structure.opening_range_15m_high` | decimal string | session |
| `structure.opening_range_15m_low` | decimal string | session |
| `structure.opening_range_30m_high/low` | decimal string | session |
| `structure.prior_day_high` | decimal string | multi-day |
| `structure.prior_day_low` | decimal string | multi-day |
| `structure.prior_day_close` | decimal string | multi-day |
| `structure.last_completed_5m` | `{open,high,low,close,bar_start_ist}` | session |
| `structure.move_vs_last_5m` | `developing\|stalling\|exhausted\|flat` | session |
| `structure.quality` | QualityFlag | session |

5m bars: aggregate from local 1m only.

---

## volatility (`features/volatility.py`)

| Field | Type | Scope |
| --- | --- | --- |
| `vol.rv_5m` | decimal string | session |
| `vol.rv_15m` | decimal string | session |
| `vol.rv_60m` | decimal string | session |
| `vol.iv_atm` | decimal string | session |
| `vol.rv_iv_ratio` | decimal string | session |
| `vol.vix_level` | decimal string | session |
| `vol.vix_change_pct` | decimal string | session |
| `vol.quality` | QualityFlag | session |

---

## order_flow (`features/order_flow.py`)

| Field | Type | Scope |
| --- | --- | --- |
| `flow.session_delta` | int | session |
| `flow.delta_5m` | int | session |
| `flow.buy_volume`, `flow.sell_volume` | int | session |
| `flow.aggression_buy_pct` | decimal string | session |
| `flow.large_print_count` | int | session |
| `flow.quality` | QualityFlag | session |

`large_print_count`: ticks with `trade_size >= large_print_threshold` from config.

---

## depth_features (`features/depth_features.py`)

| Field | Type | Scope |
| --- | --- | --- |
| `depth.imbalance_l1` | decimal string | session |
| `depth.imbalance_l5` | decimal string | session |
| `depth.spread_bps` | decimal string | session |
| `depth.as_of_utc` | datetime | session |
| `depth.quality` | QualityFlag | session |

`imbalance_l5 = (sum bid_size - sum ask_size) / (sum bid_size + sum ask_size)`.

---

## event_context (`features/event_context.py`)

**No news feeds.** Only calendar labels and whether **blackout** (no new entries) is active.
| Field | Type | Scope |
| --- | --- | --- |
| `events.event_state` | string | session |
| `events.blackout_active` | bool | session |
| `events.calendar_labels` | string[] | multi-day |
| `events.quality` | QualityFlag | session |

Source: existing discovery/event config and `EVENT_BLACKOUT` evaluation.

---

## mechanical_context (`features/mechanical_context.py`)

Read-only copy of what the **mechanical strategy** (rule-based setup detector) would do — not a gate in paper.
| Field | Type | Scope |
| --- | --- | --- |
| `mechanical.trend` | `UP\|DOWN\|RANGE` | session |
| `mechanical.trend_score` | decimal string | session |
| `mechanical.setup_would_fire` | bool | session |
| `mechanical.setup_direction` | `UP\|DOWN\|NONE` | session |
| `mechanical.mode_id` | string | session |
| `mechanical.quality` | QualityFlag | session |

---

## Done when (features module)

- One feature row exists for every minute where the minute bundle is not `FAILED`.
- Re-running the job on stored inputs gives the same output.
- `quality=STALE` is set when upstream data is old — for logs only in paper, not for entry blocking.
