# Shadow mode — optional side-by-side comparison

Follow [INDEX.md writing rules](./INDEX.md).

**Shadow mode** runs the new M2 agentic agent **in parallel** with the old five-minute veto. Paper entries still follow **one** authority until you flip a config flag.

Code folder: `src/trading/analytics/platter_shadow/`

Optional — not required for revamp “done” if M2 agentic agent is already the gate.

---

## Who controls entries

| Piece | Controls entries? | Role in shadow |
| --- | --- | --- |
| Old `m2_veto_cycle` | Yes, if `gate_authority: legacy` | May still write verdict file |
| New M2 agentic agent | Yes, if `gate_authority: m2_agentic` | Computes direction + verdict |
| `admit_m2_entry` | Reads **one** verdict file | Unchanged until promotion |

Config snippet in `config/m2_agentic_pipeline.yaml`:

```yaml
shadow_mode:
  enabled: true
  gate_authority: legacy   # legacy or M2 agentic
  log_path: data/paper/m2_agentic_shadow.jsonl
```

---

## Shadow log line (`PlatterShadowRecord`)

When both paths run at the same five-minute mark:

| Field | Type |
| --- | --- |
| `schema_version` | `"1"` |
| `m2_agentic_mark_ist` | datetime |
| `m2_agentic_id` | string |
| `legacy.action` | APPROVE, VETO, or SIZE_DOWN |
| `legacy.direction` | optional |
| `m2_agentic.action` | APPROVE or VETO (paper) |
| `m2_agentic.direction` | LONG, SHORT, NEUTRAL |
| `mechanical.setup_direction` | UP, DOWN, NONE |
| `score_total` | decimal string |
| `fail_closed` | bool |
| `divergence` | true if legacy and M2 agentic actions differ |

---

## Batch comparison report

Script: `analytics/platter_shadow/report.py`

| Metric | Meaning |
| --- | --- |
| `divergence_rate` | percent of marks where actions differ |
| `m2_agentic_veto_rate` | percent M2 agentic said VETO |
| `legacy_veto_rate` | percent legacy said VETO |
| `false_veto_proxy` | M2 agentic VETO but next 30m move matched mechanical direction |
| `missed_veto_proxy` | M2 agentic APPROVE but next 30m move went against mechanical |

Forward returns use platter spot price and later decision packets same session only.

---

## Promotion (human decision)

Defaults in config:

- At least **10** session days of shadow logs.
- Document `divergence_rate`; no automatic flip.
- Human sets `gate_authority: m2_agentic` and restarts paper.

---

## Backtest replay (no LLM required for first pass)

**Reads:** saved decision packets under `data/m2_agentic/decision_packets/`.

**Runs:** rubric only → direction → gate map.

**Writes:** CSV of marks with direction, action, forward returns.

---

## Done when

- Shadow log lines appear when `shadow_mode.enabled` and M2 agentic agent runs.
- Entry gate unchanged until `gate_authority` is m2_agentic.
