# Oracle Ubuntu VM

Persistent execution core for the data pipeline (Phase 1). Analytics stays off
the VM until Phase 6.

## Prerequisites

- Oracle Cloud Ubuntu 24.04 instance (same VM as blue-green if you prefer one
  box). System Python is 3.12; deploy uses `uv python install 3.11` — no apt
  package required.
- SSH private key in the **blue-green** repo only:

  `../blue-green/keys/ssh-key-2026-09-12.key`

  Never copy this key into `fno-automated/keys/` — it is gitignored here on
  purpose.

- Fyers API credentials in `.env` (copy from `.env.example`). **The file must
  exist locally before deploy** — `--with-secrets` uploads it to the VM.
  Alternative: `--env-file /path/to/credentials.env`.
- Obtain a token interactively (same as blue-green):
  `uv run trading auth fyers` — opens the login URL, prompts for the redirect
  URL, saves `.fyers_token`.

## Deploy from your laptop

```bash
cd /Users/apple/Documents/manasjit/fno-automated

# Replace with your VM's public IP if not 92.4.94.79
./deploy/deploy_oracle.sh \
  --host ubuntu@92.4.94.79 \
  --key ../blue-green/keys/ssh-key-2026-09-12.key \
  --with-secrets \
  --install \
  --fetch
```

What this does:

1. `rsync` the repo to `~/fno-automated` on the VM (no `--delete`).
2. `uv sync --extra data` (polars, duckdb, httpx).
3. `ruff`, `mypy`, `pytest`.
4. Optional: install `fno-data-pipeline.timer` (polls during NSE hours, UTC).
5. Optional: one `trading data fetch` to verify Fyers connectivity.

## On the VM

```bash
cd ~/fno-automated
uv run trading data fetch          # quotes, bars, chain, depth, status
uv run trading data replay --hours 24
uv run trading data stream --max-ticks 20 --duration 30
systemctl status fno-data-pipeline.timer
journalctl -u fno-data-pipeline.service -n 50
systemctl start fno-data-tick.service   # optional WS daemon
journalctl -u fno-data-tick.service -n 50
```

Stored under `data/raw/` (broker JSON) and `data/canonical/` (JSONL events).

## Macro news input

Structured classifications (not free text) live in `data/macro_news.jsonl`.
Copy the example and edit with verified source data:

```bash
mkdir -p ~/fno-automated/data
cp ~/fno-automated/config/macro_news.jsonl.example ~/fno-automated/data/macro_news.jsonl
uv run trading data news validate
```

Each fetch scores eligible records and embeds the factor in the canonical event.
Missing file means no macro feature (not neutral). Validate before deploy:

```bash
uv run trading data news validate
# file: .../data/macro_news.jsonl
# valid records: 2
```

## Security

- `.env` and `.fyers_token` are excluded from rsync unless `--with-secrets`.
- Private SSH keys are never uploaded (`keys/`, `*.key` excluded).
- Broker tokens never appear in logs or domain contracts.
