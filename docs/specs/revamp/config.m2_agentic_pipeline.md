# Settings file — `m2_agentic_pipeline.yaml`

Follow [INDEX.md writing rules](./INDEX.md).

This file holds **numbers and schedules** for the revamp pipeline. Code loads it at startup; domain logic must not hard-code these values.

Path at implement time: `config/m2_agentic_pipeline.yaml`

---

## Example file (field names exact)

```yaml
schema_version: "1"
calculation_version: "m2-agentic-v1"

schedule:
  minute_backbone_ist: "09:00-15:35"
  m2_agentic_grid_ist: "09:15-15:30 step 5m"
  agent_lag_seconds_max: 15

symbols:
  nifty_index: "NSE:NIFTY50-INDEX"
  vix_index: "NSE:INDIAVIX-INDEX"
  chain_strike_count: 25

thresholds:
  large_print_contracts: 65
  absorption_min_aggressive_volume: 1000
  absorption_max_progress_ticks: 2
  opening_range_minutes: [15, 30]

rubric_weights:
  structure: "0.25"
  flow: "0.20"
  depth: "0.15"
  vol: "0.15"
  options: "0.15"
  events: "0.10"

rubric_direction_cutoff: "1.0"

options:
  gex_formula_version: "v1-dealer-short-gamma"
  lot_size: 65

agent:
  llm_enabled: true
  model_id: deepseek-flash
  thinking: disabled

paper_mode:
  free_trade: true   # light gate; see gate.mapping.md

telemetry:
  retention_days: 45
  compact_ticks_after_days: 14
  keep_weekly_rollups_days: 365
  min_free_gb: 5

shadow_mode:
  enabled: false
  gate_authority: legacy   # legacy or M2 agentic
  log_path: data/paper/m2_agentic_shadow.jsonl
```

---

## Plain meanings

| Section | What it controls |
| --- | --- |
| `schedule` | When minute job and five-minute decision packet/agent run (IST) |
| `symbols` | Fyers symbol strings and how many strikes each side |
| `thresholds` | Large trade size, absorption detection, opening range windows |
| `rubric_weights` | Must sum to 1; how much each pillar affects the score hint |
| `rubric_direction_cutoff` | Default score level for long/short vs neutral |
| `options` | Gamma exposure formula version and lot size for options math |
| `agent.llm_enabled` | Whether to call the LLM and which model |
| `PLATTER_AGENT_LLM_ENABLED` | Env override: `1`/`true`/`yes`/`on` or `0`/`false` (optional) |
| `trading m2-agentic agent --no-llm` | Disable LLM for one run without editing YAML |
| `trading m2-agentic agent --llm` | Force LLM on for one run |
| `paper_mode.free_trade` | If true, only stale quote and blackout veto entries |
| `telemetry` | How long to keep daily folders and when to trim ticks |
| `shadow_mode` | Optional parallel run vs old veto |

---

## Validation rules

- Rubric weights sum to 1.
- `chain_strike_count` matches `config/data_pipeline.yaml`.
- `retention_days` ≥ 7.
