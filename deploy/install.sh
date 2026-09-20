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

systemctl daemon-reload
systemctl enable --now fno-data-pipeline.timer
systemctl enable --now fno-fyers-refresh.timer
systemctl enable --now fno-agent-weekly.timer
systemctl enable --now fno-agent-advise.timer
systemctl enable --now fno-agent-research.timer
systemctl enable fno-data-tick.service
systemctl enable fno-automated.service

echo "Installed."
echo "Data pipeline timer:     systemctl status fno-data-pipeline.timer"
echo "Token refresh timer:     systemctl status fno-fyers-refresh.timer"
echo "Weekly agent timer:      systemctl status fno-agent-weekly.timer"
echo "Advise desk timer:       systemctl status fno-agent-advise.timer"
echo "Research runner timer:   systemctl status fno-agent-research.timer"
echo "Supervisor daemon:       systemctl status fno-automated.service"
