# Entry gate — paper mode (light blocking)

Follow [INDEX.md writing rules](./INDEX.md).

The **gate** decides APPROVE (allow new entry at full size) or VETO (block). In paper **free-trade** mode, almost everything is APPROVE.

Code module: `src/trading/trade/m2_veto.py` (extend existing; logic must stay deterministic — no AI on the entry path).

---

## Mode flag

`config/m2_agentic_pipeline.yaml` → `paper_mode.free_trade: true` (default **true** for paper).

When true, this document applies. Stricter live-money rules come in a later spec.

---

## Inputs when the agent writes the verdict

- The decision packet (or hard-rail flags only)
- Agent `direction`: LONG, SHORT, or NEUTRAL

---

## Gate action table

| Condition | `action` | `size_fraction` |
| --- | --- | --- |
| Stale spot quote (`decision_packet.fail_closed` or `hard_rails.stale_data_veto`) | VETO | `"0"` |
| Event blackout (`events.blackout_active` or `hard_rails.event_blackout_veto`) | VETO | `"0"` |
| Missing decision packet or agent crash | VETO | `"0"` |
| **Everything else** | APPROVE | `"1"` |

Paper v1 does **not** use SIZE_DOWN. The field may exist for future live mode; paper always full size on APPROVE.

---

## Agent vs mechanical direction (log only)

Compare agent direction to `mechanical.setup_direction` from the decision packet. Store in telemetry:

| Field | Values |
| --- | --- |
| `alignment` | `aligned`, `conflict`, or `na` |

Example conflict: mechanical says UP, agent says SHORT → still **APPROVE** if hard rails pass.

---

## How entries use the verdict (unchanged shape)

1. Paper runner calls `admit_m2_entry(now)` — reads the verdict file only.
2. Gateway `lots_after_veto` — on APPROVE, full lots; on VETO, zero.

The agent never runs inside these functions.

---

## Done when

- Tests: stale quote → VETO; blackout → VETO; agent conflicts with mechanical → APPROVE.
- Telemetry shows conflicts without vetoing.
