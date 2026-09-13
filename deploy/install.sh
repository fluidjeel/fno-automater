#!/usr/bin/env bash
# Install systemd units for the data pipeline on the Oracle Ubuntu VM.
# Run on the VM: sudo ./deploy/install.sh
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_ROOT="$(pwd)"

sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/data-pipeline.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-data-pipeline.service
cp deploy/data-pipeline.timer /etc/systemd/system/fno-data-pipeline.timer
sed "s|/home/ubuntu/fno-automated|${REPO_ROOT}|g" deploy/data-tick.service \
  | sed "s|EnvironmentFile=.*|EnvironmentFile=-${REPO_ROOT}/.env|" \
  > /etc/systemd/system/fno-data-tick.service

systemctl daemon-reload
systemctl enable --now fno-data-pipeline.timer
systemctl enable fno-data-tick.service

echo "Installed. Timer status: systemctl status fno-data-pipeline.timer"
echo "One-shot fetch:        systemctl start fno-data-pipeline.service"
echo "Tick daemon (manual):  systemctl start fno-data-tick.service"
