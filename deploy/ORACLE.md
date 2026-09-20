# Oracle Ubuntu VM

Persistent execution core for the data pipeline, paper execution, and bounded Agent Desk harness.

## Prerequisites

- Oracle Cloud Ubuntu 24.04 instance (system Python is 3.12; deploy uses `uv python install 3.11` — no apt package required).
- SSH private key in the **blue-green** repo:
  `../blue-green/keys/ssh-key-2026-09-12.key`
  Never copy this key into `fno-automated/keys/` — it is gitignored here on purpose.
- Credentials in `.env` (copy from `.env.example`):
  - `DEEPSEEK_API_KEY`: for Layer 4 weekly agent, advise desk, and research loops.
  - `FYERS_APP_ID`, `FYERS_SECRET_KEY`, `FYERS_PIN`: for Fyers data feeds and token refresh.
  - `--with-secrets` uploads `.env` and tokens to the VM.
- Obtain a token interactively before first deploy:
  `uv run trading auth fyers` — opens the login URL, prompts for redirect URL, saves `.fyers_token`.

## Deploy from your laptop

```bash
cd /Users/apple/Documents/manasjit/fno-automated

# Deploy code, secrets, seed data, and install systemd timers
./deploy/deploy_oracle.sh \
  --host ubuntu@92.4.94.79 \
  --key ../blue-green/keys/ssh-key-2026-09-12.key \
  --with-secrets \
  --with-data \
  --install \
  --fetch
```

What this does:
1. `rsync` the repo to `~/fno-automated` on the VM.
2. If `--with-secrets`: syncs `.env`, `.fyers_token`, and `.fyers_refresh_token`.
3. If `--with-data`: seeds `data/macro_news.jsonl` and baseline paper cohorts to `data/paper/cohorts/`.
4. `uv sync --extra data` and executes test suite (`ruff check`, `mypy`, `pytest`).
5. If `--install`: templates and enables systemd timers:
   - Data pipeline timer (`fno-data-pipeline.timer`) — NSE hours.
   - Fyers token refresh (`fno-fyers-refresh.timer`) — daily token lifecycle.
   - Weekly agent timer (`fno-agent-weekly.timer`) — Saturdays at 10:00 UTC (15:30 IST).
   - Advise desk timer (`fno-agent-advise.timer`) — Weekdays at 03:15 UTC (08:45 IST).
   - Research loop timer (`fno-agent-research.timer`) — Sundays at 10:00 UTC (15:30 IST).
   - Supervisor daemon (`fno-automated.service`) — unattended ops daemon.
6. If `--fetch`: runs one data pipeline fetch to verify live connectivity.

## Interacting with the Agent from your Laptop

Run remote agent queries over SSH directly from your laptop. Artifacts are automatically pulled back to your local `data/paper/agent_runs/`:

```bash
# Ask the Weekly Agent (auto-pulls proposal.json, reasoning.md, and turns locally)
./scripts/oracle_agent_ask.sh

# Ask the Structure Advise Desk pre-market
./scripts/oracle_agent_advise.sh

# Synchronize all remote runs and ledgers at any time
./scripts/oracle_agent_sync.sh --all
```

## On the VM (manual checks)

```bash
cd ~/fno-automated

# Live pipeline check
uv run trading data fetch

# Run the agent weekly proposal manually
uv run trading agent weekly --enable

# Run the structure advice desk manually
uv run trading agent advise --enable

# Check systemd timers status
systemctl status fno-data-pipeline.timer
systemctl status fno-fyers-refresh.timer
systemctl status fno-agent-weekly.timer
systemctl status fno-agent-advise.timer
systemctl status fno-agent-research.timer

# View agent logs
tail -n 50 data/agent_weekly.log
tail -n 50 data/agent_advise.log
```

## Security & Governance

- Zero LLM on live order paths (deterministic kernel handles risk, limits, and fills).
- Invariant 7 demotion: Missing or expired `AuthorityGrant` demotes to `AuthorityMode.OBSERVE` without blocking execution.
- Budget caps: Monthly agent token spend is bounded and ledgered in `data/agent/budget.sqlite`.
- Private SSH keys are never uploaded (`keys/`, `*.key` excluded).
- Secrets stay strictly in `.env`.
