#!/usr/bin/env bash
# M2 agentic data stack; set INSTALL_PAPER_LIVE=1 for simulated trading units too.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

if command -v uv >/dev/null 2>&1; then
  uv sync --extra data
elif test -x "${HOME}/.local/bin/uv"; then
  "${HOME}/.local/bin/uv" sync --extra data
fi

_install_unit() {
  local src="$1"
  local dest="$2"
  sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" "$src" \
    | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
    > "/etc/systemd/system/${dest}"
}

_install_unit deploy/m2-agentic-tick.service fno-m2-agentic-tick.service
_install_unit deploy/fyers-refresh.service fno-fyers-refresh.service
cp deploy/fyers-refresh.timer /etc/systemd/system/fno-fyers-refresh.timer

_install_unit deploy/m2-agentic-minute.service fno-m2-agentic-minute.service
cp deploy/m2-agentic-minute.timer /etc/systemd/system/fno-m2-agentic-minute.timer

_install_unit deploy/m2-agentic-agent.service fno-m2-agentic-agent.service
cp deploy/m2-agentic-agent.timer /etc/systemd/system/fno-m2-agentic-agent.timer

_install_unit deploy/fno-telemetry-retention.service fno-telemetry-retention.service
cp deploy/fno-telemetry-retention.timer /etc/systemd/system/fno-telemetry-retention.timer

_install_unit deploy/fno-telemetry-weekly.service fno-telemetry-weekly.service
cp deploy/fno-telemetry-weekly.timer /etc/systemd/system/fno-telemetry-weekly.timer

systemctl daemon-reload
systemctl enable --now fno-m2-agentic-tick.service
systemctl enable --now fno-fyers-refresh.timer
systemctl enable --now fno-m2-agentic-minute.timer
systemctl enable --now fno-m2-agentic-agent.timer
systemctl enable --now fno-telemetry-retention.timer
systemctl enable --now fno-telemetry-weekly.timer

for legacy in fno-data-pipeline.timer fno-data-tick.service fno-platter-minute.timer fno-platter-agent.timer; do
  systemctl disable --now "$legacy" 2>/dev/null || true
done

if test "${INSTALL_PAPER_LIVE:-0}" = "1"; then
  _install_unit deploy/paper-session.service fno-paper-session.service
  cp deploy/paper-session.timer /etc/systemd/system/fno-paper-session.timer
  _install_unit deploy/paper-watchdog.service fno-paper-watchdog.service
  cp deploy/paper-watchdog.timer /etc/systemd/system/fno-paper-watchdog.timer
  _install_unit deploy/paper-alert.service fno-paper-alert.service
  _install_unit deploy/fno-automated.service fno-automated.service
  systemctl daemon-reload
  systemctl enable --now fno-paper-watchdog.timer
  systemctl enable --now fno-automated.service
  systemctl restart fno-m2-agentic-tick.service
  systemctl start fno-paper-session.service
  echo "PAPER live: fno-automated + paper session + m2-agentic tick under ${REPO_ROOT}"
else
  for legacy in fno-paper-session.timer fno-paper-session.service fno-automated.service fno-paper-watchdog.timer; do
    systemctl disable --now "$legacy" 2>/dev/null || true
  done
  echo "Agentic-only install complete under ${REPO_ROOT}"
fi
