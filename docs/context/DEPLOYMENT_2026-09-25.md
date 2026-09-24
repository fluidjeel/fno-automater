# Deployment record — 2026-09-25

## Source

- Repository commit (local): `47dcf15ffbdf6bb2cc0d7470c89c76746106aa1a`
- Oracle path: `/home/ubuntu/fno-automated` (rsync deploy, no `.git`)

## Commands executed

| Step | Command | Result |
| --- | --- | --- |
| Baseline SSH | `ssh ubuntu@92.4.94.79 hostname; systemctl is-active ...` | `fno-automated` + `fno-data-tick` active; `fno-paper-session` inactive (off-hours) |
| Entry freeze | Python sqlite query on `data/paper/trading.sqlite` | `None` (no freeze row) |
| Deploy | `./deploy/deploy_oracle.sh --host ubuntu@92.4.94.79 --with-secrets` | Code + configs synced |
| VM tests | `uv run pytest tests/test_four_mode_session_integration.py tests/test_cas_event_path.py` | 6 passed |
| Instruments | `uv run trading data backfill instruments` | NSE_FO 81731 specs (2026-09-24 capture) |
| Activate four-mode | `cp config/paper_session_four_mode.yaml config/paper_session.yaml` | md5 `b328db8887145b19f1e2232c60153785` |
| G1 (local) | `uv run trading risk one-lot` | 14/18 affordable at lot 65 |

## Config versions active on Oracle

- `config/paper.yaml` — PAPER account identity (unchanged)
- `config/paper_session.yaml` — four_mode routing, M3/M4 PAPER
- `config/modes.yaml` — four-mode capital policy
- `config/modes.yaml` + `config/risk.yaml` — Layer 2 limits

## Rollback

```bash
cp config/paper_session_legacy.yaml config/paper_session.yaml
sudo systemctl restart fno-paper-session.service
```
