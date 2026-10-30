# M2-agentic branch

This branch keeps only the live data pipeline, the five-minute scoring agent,
the approval check file, and decision logging. Simulated intraday trading loop
code, the old discovery session, and legacy deploy schedules were removed.

## What stays

- Tick stream, minute REST snapshots, feature rows, decision JSON assembly
- Five-minute agent (optional LLM), approval mapping, log append
- Config: `config/platter_pipeline.yaml`, `config/m2_veto.yaml`, `config/data_pipeline.yaml`
- Deploy: tick service, Fyers refresh, minute+features timer, agent timer, log cleanup timers
- Tests: `tests/test_platter_*.py`, `tests/test_m2_veto.py`

## CLI

```bash
uv run trading auth telegram
uv run trading platter tick-daemon
uv run trading platter minute
uv run trading platter agent
```

## Oracle

```bash
sudo ./deploy/install.sh
```
