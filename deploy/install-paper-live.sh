#!/usr/bin/env bash
# Install M2 agentic data stack + unattended PAPER trading on the same host.
set -euo pipefail

cd "$(dirname "$0")"
INSTALL_AGENTIC=1 INSTALL_PAPER_LIVE=1 ./install.sh