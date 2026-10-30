# Agent — what it reads and writes

Follow [INDEX.md writing rules](./INDEX.md).

The **agent** turns one platter JSON into a **direction** (long, short, or neutral) and a small **verdict file** the entry gate reads. It does **not** place orders.

Code folder: `src/trading/ai/platter_agent/`

---

## Input (only this)

One `DecisionPlatter` JSON (schema v1). No broker calls inside the agent.

If there is no platter for the current mark → write **VETO** (block entry), telemetry flag `missing_platter=true`.

---

## Agent decision output

| Field | Type | Required |
| --- | --- | --- |
| `direction` | enum | **One of:** `LONG`, `SHORT`, `NEUTRAL` |

Also log to [telemetry](./telemetry.md):

| Field | Type |
| --- | --- |
| `pillar_scores.*` | int −2..+2 (use 0 if data missing) |
| `score_total` | decimal string |
| `confidence` | decimal string 0..1 |
| `reasons` | string[] (at least one line) |

---

## Score rubric (soft in paper)

Weights come from config. These cutoffs are **defaults**, not hard law.

| Condition | default direction |
| --- | --- |
| `score_total >= +1.0` | LONG |
| `score_total <= -1.0` | SHORT |
| else | NEUTRAL |

**Paper rules:**

- Missing optional pillars → score 0 for that pillar; **do not** force neutral.
- If `platter.fail_closed` (stale spot quote only) → skip LLM, direction NEUTRAL, reason mentions stale quote.

---

## LLM overlay (`agent.llm_enabled` in config)

**LLM** = large language model API call.

- Model: `deepseek-flash`, extended thinking off.
- In paper, the model may pick long/short/neutral freely from the full platter; the rubric is a hint in the prompt only.
- Bad JSON from the model → NEUTRAL plus reason; gate still **APPROVE** unless a hard rail fires.

---

## Verdict file (what the gate reads)

Path stays `data/paper/m2_veto_verdict.json` (same as today’s five-minute check).

| Field | Type |
| --- | --- |
| `action` | `APPROVE` or `VETO` (paper: no size-down) |
| `size_fraction` | `"1"` or `"0"` |
| `direction` | LONG, SHORT, or NEUTRAL |
| `reasons` | string[] |
| `as_of`, `valid_until` | datetime |
| `source` | `"platter-agent-v1"` |
| `platter_id` | string |
| `decision_id` | string (same id as telemetry line) |

How `action` is chosen: [gate.mapping.md](./gate.mapping.md).

---

## Telemetry (required every run)

Append one [AgentDecisionRecord](./telemetry.md) line **before** the agent finishes.

---

## Schedule

Every **five minutes** IST at the platter mark, within 15 seconds.

Verdict stays valid **300 seconds** unless config changes (`config/m2_veto.yaml`).

---

## Done when

- Agent code imports no broker or exit modules.
- Fresh quote and no blackout → **APPROVE** for all directions.
- Every run adds one telemetry line.
