# Shadow mode — optional side-by-side comparison

Follow [INDEX.md writing rules](./INDEX.md).

**Shadow mode** runs the new platter agent **in parallel** with the old five-minute veto. Paper entries still follow **one** authority until you flip a config flag.

Code folder: `src/trading/analytics/platter_shadow/`

Optional — not required for revamp “done” if platter agent is already the gate.

---

## Who controls entries

| Piece | Controls entries? | Role in shadow |
| --- | --- | --- |
| Old `m2_veto_cycle` | Yes, if `gate_authority: legacy` | May still write verdict file |
| New platter agent | Yes, if `gate_authority: platter` | Computes direction + verdict |
| `admit_m2_entry` | Reads **one** verdict file | Unchanged until promotion |

Config snippet in `config/platter_pipeline.yaml`:

```yaml
shadow_mode:
  enabled: true
  gate_authority: legacy   # legacy or platter
  log_path: data/paper/platter_shadow.jsonl
```

---

## Shadow log line (`PlatterShadowRecord`)

When both paths run at the same five-minute mark:

| Field | Type |
| --- | --- |
| `schema_version` | `"1"` |
| `platter_mark_ist` | datetime |
| `platter_id` | string |
| `legacy.action` | APPROVE, VETO, or SIZE_DOWN |
| `legacy.direction` | optional |
| `platter.action` | APPROVE or VETO (paper) |
| `platter.direction` | LONG, SHORT, NEUTRAL |
| `mechanical.setup_direction` | UP, DOWN, NONE |
| `score_total` | decimal string |
| `fail_closed` | bool |
| `divergence` | true if legacy and platter actions differ |

---

## Batch comparison report

Script: `analytics/platter_shadow/report.py`

| Metric | Meaning |
| --- | --- |
| `divergence_rate` | percent of marks where actions differ |
| `platter_veto_rate` | percent platter said VETO |
| `legacy_veto_rate` | percent legacy said VETO |
| `false_veto_proxy` | platter VETO but next 30m move matched mechanical direction |
| `missed_veto_proxy` | platter APPROVE but next 30m move went against mechanical |

Forward returns use platter spot price and later platters same session only.

---

## Promotion (human decision)

Defaults in config:

- At least **10** session days of shadow logs.
- Document `divergence_rate`; no automatic flip.
- Human sets `gate_authority: platter` and restarts paper.

---

## Backtest replay (no LLM required for first pass)

**Reads:** saved platters under `data/platter/platters/`.

**Runs:** rubric only → direction → gate map.

**Writes:** CSV of marks with direction, action, forward returns.

---

## Done when

- Shadow log lines appear when `shadow_mode.enabled` and platter agent runs.
- Entry gate unchanged until `gate_authority` is platter.
