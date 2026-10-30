# Trading revamp — spec index

## Writing rules (every file in this folder)

These rules apply to **all** specs under `docs/specs/revamp/`:

1. **No jargon.** Use everyday words. If a technical word is required, define it in one short line the first time it appears.
2. **Short sentences.** One idea per sentence when possible.
3. **Names stay exact.** File paths, field names, and config keys stay spelled exactly as in code — but explain what they mean in plain English beside or below them.
4. **Reader needs no prior context.** Assume the reader has not read the codebase or earlier chats.
5. **Paper first.** This revamp targets **paper trading** (simulated money). Live-money rules come later in a separate spec update.

News feeds are **not** in scope. Only the **event calendar** and **event blackout** (no new trades during certain events) are included.

When specs disagree with old chat messages, **these spec files win**.

---

## What we are building (one paragraph)

We collect market data from the broker (Fyers), build a rich snapshot every five minutes during the Indian market session, ask an **agent** (AI plus simple scoring) for **long**, **short**, or **neutral**, save everything for later review, and use **minimal blocking** on new trades in paper mode — only bad stale prices and event blackout stop entries. Stop-loss and take-profit stay in existing deterministic code, not in the agent.

---

## Read specs in this order

| Step | File | What it describes |
| --- | --- | --- |
| 1 | [00-global.md](./00-global.md) | Session times, safety rails, broker limits, folder paths |
| 2 | [tick_consumer.md](./tick_consumer.md) | Live price stream → one-minute bars and buy/sell volume |
| 3 | [minute_backbone.md](./minute_backbone.md) | Once per minute: option chain and order book snapshot |
| 4 | [features.md](./features.md) | Computed signals (VWAP, gamma exposure, etc.) |
| 5 | [platter.schema.md](./platter.schema.md) | The single JSON shape for each five-minute snapshot |
| 6 | [platter_assemble.md](./platter_assemble.md) | Builds that JSON every five minutes |
| 7 | [agent.contract.md](./agent.contract.md) | Agent input/output and schedule |
| 8 | [gate.mapping.md](./gate.mapping.md) | When a new trade is allowed vs blocked (paper mode) |
| 9 | [telemetry.md](./telemetry.md) | Save every decision, rotate old data, weekly reports |
| 10 | [exit.engine.contract.md](./exit.engine.contract.md) | Stops and targets (unchanged; agent does not touch this) |
| 11 | [shadow.contract.md](./shadow.contract.md) | Optional comparison with the old veto system |
| 12 | [config.platter_pipeline.md](./config.platter_pipeline.md) | Settings file field meanings |

---

## Config files (settings on disk)

| File | Purpose |
| --- | --- |
| `config/platter_pipeline.yaml` | Schedules, weights, retention days, paper mode |
| [config.platter_pipeline.md](./config.platter_pipeline.md) | Plain explanation of each setting |
| `config/data_pipeline.yaml` | Existing data freshness limits (do not weaken) |
| `config/m2_veto.yaml` | Verdict file location and model name |

---

## Done when (acceptance)

1. One full trading day produces ticks, minute files, feature files, platters every five minutes, agent answers, and one telemetry log file for that day.
2. New M2 entries are **allowed** in paper unless the spot quote is too old or event blackout is on.
3. A weekly summary file is produced with every decision and paths to replay inputs.
4. A daily cleanup job keeps disk usage stable.
