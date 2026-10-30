#!/usr/bin/env bash
# Install systemd units for the data pipeline and agent harness on the Oracle Ubuntu VM.
# Run on the VM: sudo ./deploy/install.sh
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

# Data pipeline & token refresh units
sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/data-pipeline.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-data-pipeline.service
cp deploy/data-pipeline.timer /etc/systemd/system/fno-data-pipeline.timer

sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/data-tick.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-data-tick.service

sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/fyers-refresh.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-fyers-refresh.service
cp deploy/fyers-refresh.timer /etc/systemd/system/fno-fyers-refresh.timer

# Agent harness units
sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/agent-weekly.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-agent-weekly.service
cp deploy/agent-weekly.timer /etc/systemd/system/fno-agent-weekly.timer

sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/agent-advise.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-agent-advise.service
cp deploy/agent-advise.timer /etc/systemd/system/fno-agent-advise.timer

sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/agent-research.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-agent-research.service
cp deploy/agent-research.timer /etc/systemd/system/fno-agent-research.timer

# Supervisor daemon
sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/fno-automated.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-automated.service

# PAPER session (enabled only; start after credentials are confirmed)
sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/paper-session.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-paper-session.service

sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/paper-alert.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-paper-alert.service

install -m 0440 deploy/fno-systemctl.sudoers /etc/sudoers.d/fno-systemctl
visudo -cf /etc/sudoers.d/fno-systemctl

cp deploy/paper-session.timer /etc/systemd/system/fno-paper-session.timer

sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/paper-post-open-check.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-paper-post-open-check.service
cp deploy/paper-post-open-check.timer /etc/systemd/system/fno-paper-post-open-check.timer

systemctl daemon-reload
systemctl enable --now fno-data-pipeline.timer
systemctl enable --now fno-fyers-refresh.timer
systemctl enable --now fno-agent-weekly.timer
systemctl enable --now fno-agent-advise.timer
systemctl enable --now fno-agent-research.timer
systemctl enable --now fno-data-tick.service
systemctl enable --now fno-automated.service
systemctl enable fno-paper-session.service
systemctl enable --now fno-paper-session.timer
systemctl enable --now fno-paper-post-open-check.timer

sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/paper-watchdog.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-paper-watchdog.service
cp deploy/paper-watchdog.timer /etc/systemd/system/fno-paper-watchdog.timer
systemctl enable --now fno-paper-watchdog.timer

# Platter revamp pipeline (paper data + agent + telemetry)
sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/platter-minute.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-platter-minute.service
cp deploy/platter-minute.timer /etc/systemd/system/fno-platter-minute.timer

sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/platter-agent.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-platter-agent.service
cp deploy/platter-agent.timer /etc/systemd/system/fno-platter-agent.timer

sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/fno-telemetry-retention.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-telemetry-retention.service
cp deploy/fno-telemetry-retention.timer /etc/systemd/system/fno-telemetry-retention.timer

sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/fno-telemetry-weekly.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-telemetry-weekly.service
cp deploy/fno-telemetry-weekly.timer /etc/systemd/system/fno-telemetry-weekly.timer

systemctl daemon-reload
systemctl enable --now fno-platter-minute.timer
systemctl enable --now fno-platter-agent.timer
systemctl enable --now fno-telemetry-retention.timer
systemctl enable --now fno-telemetry-weekly.timer

echo "Installed."
echo "Data pipeline timer:     systemctl status fno-data-pipeline.timer"
echo "Token refresh timer:     systemctl status fno-fyers-refresh.timer"
echo "Weekly agent timer:      systemctl status fno-agent-weekly.timer"
echo "Advise desk timer:       systemctl status fno-agent-advise.timer"
echo "Research runner timer:   systemctl status fno-agent-research.timer"
echo "Supervisor daemon:       systemctl status fno-automated.service"
echo "Paper session:           systemctl status fno-paper-session.service"
echo "Paper session timer:     systemctl status fno-paper-session.timer"
echo "Post-open check timer:   systemctl status fno-paper-post-open-check.timer"
echo "Paper watchdog timer:    systemctl status fno-paper-watchdog.timer"
echo "Platter minute timer:    systemctl status fno-platter-minute.timer"
echo "Platter agent timer:     systemctl status fno-platter-agent.timer"
echo "Telemetry retention:     systemctl status fno-telemetry-retention.timer"
echo "Telemetry weekly timer:  systemctl status fno-telemetry-weekly.timer"
