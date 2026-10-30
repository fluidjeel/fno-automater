#!/usr/bin/env bash
# Agentic stack only: tick stream, Fyers refresh, minute/features, five-minute agent, logs.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

_install_unit() {
  local src="$1"
  local dest="$2"
  sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" "$src" \
    | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
    > "/etc/systemd/system/${dest}"
}

_install_unit deploy/data-tick.service fno-data-tick.service
_install_unit deploy/fyers-refresh.service fno-fyers-refresh.service
cp deploy/fyers-refresh.timer /etc/systemd/system/fno-fyers-refresh.timer

_install_unit deploy/platter-minute.service fno-platter-minute.service
cp deploy/platter-minute.timer /etc/systemd/system/fno-platter-minute.timer

_install_unit deploy/platter-agent.service fno-platter-agent.service
cp deploy/platter-agent.timer /etc/systemd/system/fno-platter-agent.timer

_install_unit deploy/fno-telemetry-retention.service fno-telemetry-retention.service
cp deploy/fno-telemetry-retention.timer /etc/systemd/system/fno-telemetry-retention.timer

_install_unit deploy/fno-telemetry-weekly.service fno-telemetry-weekly.service
cp deploy/fno-telemetry-weekly.timer /etc/systemd/system/fno-telemetry-weekly.timer

systemctl daemon-reload
systemctl enable --now fno-data-tick.service
systemctl enable --now fno-fyers-refresh.timer
systemctl enable --now fno-platter-minute.timer
systemctl enable --now fno-platter-agent.timer
systemctl enable --now fno-telemetry-retention.timer
systemctl enable --now fno-telemetry-weekly.timer

# Simulated intraday session stays off on agentic installs.
for legacy in fno-paper-session.timer fno-paper-session.service fno-automated.service fno-data-pipeline.timer; do
  sudo systemctl disable --now "$legacy" 2>/dev/null || true
done

echo "Agentic install complete under ${REPO_ROOT}"
