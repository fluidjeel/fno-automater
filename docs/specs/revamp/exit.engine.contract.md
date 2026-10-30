# Exits — stops and targets (unchanged by revamp)

Follow [INDEX.md writing rules](./INDEX.md).

**Exits** close or reduce open trades using fixed rules (time, profit target, stop loss, trailing). The agent and platter **must not** change this path.

Code: `src/trading/trade/exits.py`, `discovery_exits.py`, `ProtectionCoordinator` — no imports from platter or LLM packages.

---

## Who is in charge

Stops, targets, time exits, and trailing profit rules run on **every price tick** from open **leg** prices and saved **exit policy** state only.

Do **not** read agent direction, platter JSON, or verdict files here.

---

## M2 strategy exit checks (order)

For strategy-level profit and loss on the open M2 option trade:

1. **Time exit:** clock reached `exit_policy.time_exit` → exit reason TIME.
2. **Stop:** unrealized loss at or below `exit_policy.pnl_stop` → STOP.
3. **Target:** unrealized profit at or above `exit_policy.pnl_target` → TARGET.
4. **Trailing stop:** after trailing logic updates, if profit falls to trail level → STOP.

---

## Trailing stop (discovery config)

Function: `apply_discovery_trailing(...)`.

Needs `premium_scaled` true and positive `structure_basis`.

**Activate trailing when:** profit ≥ `structure_basis × contracts × trail_activate_fraction`.

**Then:** track high-water profit; trail stop = high water × (1 − giveback fraction); never loosen an existing stop.

Uses **fresh** unrealized profit from leg mids at tick time.

---

## Discovery confirmation

If discovery exits are on: the exit signal must repeat for `exit_confirm_required` checks before sending close orders (existing helper).

---

## Per-leg price exits (non-M2)

Unchanged: compare mid to leg stop and target prices.

---

## Missing price

If monitor price missing → no exit signal, reason PRICE_UNAVAILABLE — do not panic-flatten unless existing degraded rules say so.

---

## Done when

- Exit modules import nothing from `trading.ai.platter_agent`.
- Replay of an October 2026 paper trade matches pre-revamp exit times on the same quotes.
- Deleting the agent verdict file does not change exits.
