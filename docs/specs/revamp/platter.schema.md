# Decision platter — JSON shape (contract)

Follow [INDEX.md writing rules](./INDEX.md).

A **decision platter** is the full market snapshot for one five-minute decision. The agent reads only this file (plus config), not live broker APIs.

One JSON file per mark.  
Path: `data/platter/platters/{session_date}/platter_{HHMMSS}.json`  
Example: `091500` = 09:15:00 IST.

**Schema id:** `decision_platter_v1`

## Root

| Field | Type | Scope | Writer module |
| --- | --- | --- | --- |
| `schema_version` | string const `"1"` | — | platter_assemble |
| `platter_id` | string (uuid) | session | platter_assemble |
| `platter_mark_ist` | datetime | session | platter_assemble |
| `session_date` | date string | session | platter_assemble |
| `as_of_utc` | datetime | session | platter_assemble |
| `fail_closed` | bool | session | platter_assemble |
| `freshness` | object | session | platter_assemble |
| `spot` | object | session | platter_assemble |
| `flow` | object | session | features/order_flow |
| `depth` | object | session | features/depth_features |
| `structure` | object | mixed | features/session_structure |
| `volume_profile` | object | session | features/volume_profile |
| `vwap` | object | mixed | features/vwap |
| `vol` | object | session | features/volatility |
| `options` | object | session | features/options_positioning |
| `absorption` | object | session | features/absorption |
| `events` | object | mixed | features/event_context |
| `mechanical` | object | session | features/mechanical_context |
| `lineage` | object | session | platter_assemble |

### `freshness` (platter_assemble aggregates)

| Field | Type |
| --- | --- |
| `quote_ok` | bool |
| `chain_ok` | bool |
| `depth_ok` | bool |
| `tick_ok` | bool |
| `bar_1m_ok` | bool |
| `quote_age_ms` | int |
| `chain_age_ms` | int |
| `depth_age_ms` | int |
| `tick_age_ms` | int |

`fail_closed = true` **only** when `freshness.quote_ok` is false (Nifty spot quote older than policy) or platter assembly cannot provide `spot.ltp`. Other freshness flags are recorded but do not set `fail_closed`.

### `spot`

| Field | Type | Writer |
| --- | --- | --- |
| `symbol` | string | merge |
| `ltp`, `bid`, `ask` | decimal string | merge |
| `spread_bps` | decimal string | merge |
| `source` | `ws\|rest` | merge |

### `lineage`

| Field | Type |
| --- | --- |
| `minute_bundle_path` | string |
| `feature_row_path` | string |
| `chain_snapshot_id` | string |
| `depth_snapshot_id` | string |
| `tick_store_session_date` | string |
| `feature_calc_version` | string |
| `gex_formula_version` | string |

## Nested objects

Copy **exact field names** from [features.md](./features.md) under keys:

- `flow.*`, `depth.*`, `structure.*`, `volume_profile.*`, `vwap.*`, `vol.*`, `options.*`, `absorption.*`, `events.*`, `mechanical.*`

No additional fields in v1 without schema_version bump.

## Done when

- Future code validates JSON against a strict model named `DecisionPlatter`.
- Every five-minute mark 09:15–15:30 IST has one platter file when daemons are healthy.
- You can rebuild context from `lineage` paths alone.
