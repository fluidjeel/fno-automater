# Telemetry — save every decision for weekly review

Follow [INDEX.md writing rules](./INDEX.md).

**Telemetry** = append-only logs on the Oracle server so we can replay what the agent saw and said. Old data is deleted automatically so the disk does not fill.

Code folder: `src/trading/analytics/platter_telemetry/` plus systemd timers for cleanup and weekly rollup.

**Required** for revamp “done” alongside pipeline daemons.

---

## Record shape: `AgentDecisionRecord`

One record per five-minute agent run (including VETO without calling the LLM).

| Field | Type | Required |
| --- | --- | --- |
| `schema_version` | `"1"` | yes |
| `decision_id` | string (uuid) | yes |
| `session_date` | date string IST | yes |
| `platter_mark_ist` | datetime | yes |
| `platter_id` | string | yes |
| `as_of_utc` | datetime | yes |
| `environment` | `"PAPER"` | yes |
| `platter_snapshot_path` | string | yes — path to full platter JSON |
| `platter_snapshot_inline` | object | optional; skip if platter > 512KB |
| `agent_output` | object | yes |
| `gate_output` | object | yes |
| `hard_rails` | object | yes |
| `outcome` | object | filled later by batch job |

### `agent_output`

| Field | Type |
| --- | --- |
| `direction` | LONG, SHORT, or NEUTRAL |
| `score_total` | decimal string |
| `pillar_scores` | object |
| `confidence` | decimal string |
| `reasons` | string[] |
| `llm_used` | bool |
| `model_id` | string |

### `gate_output`

| Field | Type |
| --- | --- |
| `action` | APPROVE or VETO |
| `size_fraction` | decimal string (paper: `"1"` on APPROVE) |
| `paper_mode` | bool |

### `hard_rails`

| Field | Type |
| --- | --- |
| `stale_data_veto` | bool |
| `event_blackout_veto` | bool |
| `missing_platter` | bool |

### `outcome` (filled later)

After the session, a job adds trade results:

| Field | Type |
| --- | --- |
| `mechanical_trade_attempted` | bool |
| `trade_id` | string or null |
| `entry_within_minutes` | int or null |
| `forward_return_5m_pct` | decimal string or null |
| `forward_return_30m_pct` | decimal string or null |
| `realized_pnl_inr` | decimal string or null if closed |
| `outcome_labeled_at_utc` | datetime or null |

---

## Where files live (repo root on Oracle)

| What | Path |
| --- | --- |
| Platter JSON (canonical) | `data/platter/platters/{session_date}/platter_{HHMMSS}.json` |
| Decision log (one line per run) | `data/telemetry/agent_decisions/{session_date}/decisions.jsonl` |
| Optional full copy per id | `data/telemetry/agent_decisions/{session_date}/{decision_id}.json` |
| Weekly summary | `data/telemetry/weekly/agent_week_{YYYY-Www}.json` |

**Write order:** platter file must exist, then append one JSONL line.

---

## Retention and rotation (automatic)

Settings in `config/platter_pipeline.yaml` under `telemetry`:

| Key | Default | Meaning |
| --- | --- | --- |
| `retention_days` | `45` | Delete daily folders older than this |
| `compact_ticks_after_days` | `14` | Drop raw ticks; keep one-minute bars |
| `keep_weekly_rollups_days` | `365` | Keep weekly summaries longer |
| `min_free_gb` | `5` | Emergency trim if free disk below this |

**Timer:** `fno-telemetry-retention.timer` — daily 02:00 IST:

1. Remove tick folders older than retention.
2. Remove decision folders older than retention (after weekly export if needed).
3. Remove minute/feature folders past retention; keep platters at least `retention_days`.
4. Append bytes freed to `data/telemetry/retention.log`.

Job must be safe to run twice (idempotent).

---

## Weekly analytics (required)

**Timer:** `fno-telemetry-weekly.timer` — Saturday 16:00 IST.

**Reads:** All `decisions.jsonl` for Mon–Fri that ISO week.

**Writes:** `data/telemetry/weekly/agent_week_{YYYY-Www}.json` with:

| Section | Content |
| --- | --- |
| `summary` | counts by direction; veto counts by reason |
| `decisions` | list with id, time, direction, action, top reason, outcome fields |
| `replay_index` | map decision_id → platter path for replay CLI |

**CLI (spec only):** `trading analytics agent-replay --week 2026-W44 --decision-id …` prints platter + agent output + outcome.

---

## Done when

- Any decision in the week can be replayed until retention deletes it.
- Weekly file lists every `decision_id` for that week.
- Disk use stays flat over ~60 days with defaults.

---

## Shadow mode

If shadow logging is on, it may point at the same platter paths; **telemetry is the main archive** for weekly review.
