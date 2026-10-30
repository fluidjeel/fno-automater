# Build the five-minute decision packet — specification

Follow [INDEX.md writing rules](./INDEX.md).

**Platter** = one JSON file that bundles everything the agent needs at a five-minute clock mark (see [m2_agentic.schema.md](./m2_agentic.schema.md)).

Code folder: `src/trading/data/m2_agentic/`

---

## assemble

| | |
| --- | --- |
| **When** | Every five minutes on the IST grid in [00-global.md](./00-global.md) |
| **Reads** | Latest feature row for that mark, delta state, websocket health, mechanical snapshot |
| **Writes** | One `DecisionPlatter` JSON |
| **Missing feature row** | Still write a platter with `fail_closed=true` only if spot quote missing; set freshness flags honestly |

---

## store

Write atomically: save to `.tmp`, then rename so readers never see half a file.

---

## Done when

- M2 agentic times increase in order (`m2_agentic_mark_ist`).
- `lineage.feature_row_path` points to a real file.
- Assemble path makes **no** REST historical bar calls.
