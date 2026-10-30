# M2-agentic branch

This branch keeps only the live data pipeline, the five-minute scoring agent,
the approval check file, and decision logging. Simulated intraday trading loop
code, the old discovery session, and legacy deploy schedules were removed.

## What stays

- Tick stream, minute REST snapshots, feature rows, decision JSON assembly
- Five-minute agent (optional LLM), approval mapping, log append
- Config: `config/m2_agentic_pipeline.yaml`, `config/m2_veto.yaml`, `config/data_pipeline.yaml`
- Deploy: tick service, Fyers refresh, minute+features timer, agent timer, log cleanup timers
- Tests: `tests/test_m2_agentic_*.py`, `tests/test_m2_veto.py`

## CLI

```bash
uv run trading auth telegram
uv run trading m2-agentic tick-daemon
uv run trading m2-agentic minute
uv run trading m2-agentic agent
```

## Oracle

```bash
sudo ./deploy/install.sh
```

## Removed in agentic cleanup

- Ops orchestrator (old supervisor that started simulated trading)
- Simulated session runner, watchdog, Telegram trade copy module
- `config/paper_session.yaml`
