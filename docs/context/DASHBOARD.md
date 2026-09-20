# Oracle Desk Terminal

The dashboard is a read-only local control plane for the trading runtime. It
does not import a broker transaction adapter, place orders, release entry
freezes, edit configuration, or promote a strategy.

## Why this is custom

Prometheus/Grafana are good future additions for CPU, memory, disk, process and
latency time series. They cannot reconstruct this system's domain decisions:
strike candidates, strategy abstentions, Layer 2 sizing, the binding allocation
limit, reservation compare-and-swap state, idempotency, broker reconciliation,
protection continuity, or agent authority. The terminal therefore builds a
domain-specific read model from the same durable evidence used for recovery.

## Run locally

Inspect local repository evidence:

```bash
uv run trading dashboard serve
```

Read Oracle through SSH. The local app first uses an installed probe, then falls
back to streaming the read-only collector over stdin when it is not deployed:

```bash
uv run trading dashboard serve \
  --oracle-host ubuntu@<oracle-ip> \
  --ssh-key ../blue-green/keys/<oracle-key>.key \
  --oracle-root '~/fno-automated'
```

The equivalent environment settings are:

```text
TRADING_DASHBOARD_ORACLE_HOST
TRADING_DASHBOARD_SSH_KEY
TRADING_DASHBOARD_ORACLE_ROOT
```

The browser binds to `127.0.0.1` by default. The private key remains in the
local SSH client; it is not sent to the browser or included in the telemetry
snapshot. Remote snapshot collection uses `BatchMode=yes` and is read-only. The
fallback collector executes from stdin and does not install or overwrite files
on Oracle.

For a machine-readable probe:

```bash
uv run trading dashboard snapshot --source oracle
```

## Evidence semantics

- **Observed** means a durable event, file, heartbeat or service state exists.
- **Configured** means policy or code is present but runtime use is unproven.
- **No evidence** means the terminal will not infer health from silence.
- **Shadow** and **disabled** are deliberate non-execution states.
- **Missing** is an observability or operational gap.

The terminal reads redacted configuration, systemd status, data-artifact
freshness, the SQLite trading store, protection heartbeat and Layer 4 run
artifacts. Fields whose names indicate credentials, tokens, passwords, secrets
or auth codes are redacted before serialization.

## Current monitoring gaps surfaced by the terminal

1. The paper session has no dedicated supervised systemd unit.
2. Software-only stops and a 60-second poll are not live-safe.
3. Portfolio Greeks, correlated shocks and margin headroom are not persisted as
   a time series.
4. Feed-to-fill stage latency, partial-fill/legging duration and broker reject
   metrics are not recorded as histograms.
5. Reservation CAS attempts, lock wait and duplicate-suppression outcomes are
   not explicit operational events.
6. Decision-time IV/Greeks need model/vendor, timestamp, units, inputs and
   convergence provenance.
7. Configuration and canonical documentation can drift; the current charge
   verification mismatch is shown as an example.
8. Oracle deployment currently uses an rsync checkout without a persisted Git
   revision, so release identity must be added before production promotion.

These gaps are not silently filled with synthetic values. The coverage audit
keeps them visible until the production evidence contracts are implemented.
