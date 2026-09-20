#!/usr/bin/env bash
# Bi-directional sync tool between local laptop and Oracle VM for agent runs,
# cohorts, macro news, and decision stores.
#
# Usage:
#   ./scripts/oracle_agent_sync.sh --pull           # Pull all remote agent runs to local (default)
#   ./scripts/oracle_agent_sync.sh --push-data      # Push macro news and paper cohorts to VM
#   ./scripts/oracle_agent_sync.sh --pull-db        # Pull remote trading.sqlite and budget.sqlite to local
#   ./scripts/oracle_agent_sync.sh --all            # Complete bi-directional synchronization

set -euo pipefail

HOST="${VM_HOST:-ubuntu@92.4.94.79}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="${SSH_KEY:-$REPO_ROOT/../blue-green/keys/ssh-key-2026-09-12.key}"
REMOTE_DIR="${REMOTE_DIR:-fno-automated}"

ACTION="pull"

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --pull) ACTION="pull"; shift ;;
    --push-data) ACTION="push-data"; shift ;;
    --pull-db) ACTION="pull-db"; shift ;;
    --all) ACTION="all"; shift ;;
    --host) HOST="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

[[ -f "$KEY" ]] || { echo "ERROR: SSH key not found: $KEY" >&2; exit 1; }
chmod 600 "$KEY" 2>/dev/null || true

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15
          -o ServerAliveInterval=30 -i "$KEY")

do_pull() {
  echo "==> pulling agent runs from $HOST:$REMOTE_DIR/data/paper/agent_runs/ to local"
  mkdir -p "$REPO_ROOT/data/paper/agent_runs" "$REPO_ROOT/data/agent_runs"
  rsync -avz -e "ssh ${SSH_OPTS[*]}" \
    "$HOST:$REMOTE_DIR/data/paper/agent_runs/" "$REPO_ROOT/data/paper/agent_runs/" || true
  rsync -avz -e "ssh ${SSH_OPTS[*]}" \
    "$HOST:$REMOTE_DIR/data/agent_runs/" "$REPO_ROOT/data/agent_runs/" || true
  echo "✅ agent runs pulled"
}

do_push_data() {
  echo "==> pushing macro news and paper cohorts to $HOST:$REMOTE_DIR/data"
  ssh "${SSH_OPTS[@]}" "$HOST" \
    "mkdir -p '$REMOTE_DIR/data/paper/cohorts' '$REMOTE_DIR/data/paper/agent_runs' '$REMOTE_DIR/data/agent_runs' '$REMOTE_DIR/data/agent'"
  if [[ -f "$REPO_ROOT/data/macro_news.jsonl" ]]; then
    rsync -avz -e "ssh ${SSH_OPTS[*]}" \
      "$REPO_ROOT/data/macro_news.jsonl" "$HOST:$REMOTE_DIR/data/macro_news.jsonl"
  fi
  if [[ -d "$REPO_ROOT/data/paper/cohorts" ]] && [[ "$(ls -A "$REPO_ROOT/data/paper/cohorts" 2>/dev/null)" ]]; then
    rsync -avz -e "ssh ${SSH_OPTS[*]}" \
      "$REPO_ROOT/data/paper/cohorts/" "$HOST:$REMOTE_DIR/data/paper/cohorts/"
  fi
  echo "✅ data pushed"
}

do_pull_db() {
  echo "==> pulling sqlite ledgers from $HOST:$REMOTE_DIR"
  mkdir -p "$REPO_ROOT/data/agent" "$REPO_ROOT/data/paper"
  rsync -avz -e "ssh ${SSH_OPTS[*]}" \
    "$HOST:$REMOTE_DIR/data/agent/budget.sqlite" "$REPO_ROOT/data/agent/budget.sqlite" 2>/dev/null || true
  rsync -avz -e "ssh ${SSH_OPTS[*]}" \
    "$HOST:$REMOTE_DIR/data/trading.sqlite" "$REPO_ROOT/data/trading.sqlite" 2>/dev/null || true
  rsync -avz -e "ssh ${SSH_OPTS[*]}" \
    "$HOST:$REMOTE_DIR/data/paper/trading.sqlite" "$REPO_ROOT/data/paper/trading.sqlite" 2>/dev/null || true
  echo "✅ sqlite ledgers pulled"
}

case "$ACTION" in
  pull)
    do_pull
    ;;
  push-data)
    do_push_data
    ;;
  pull-db)
    do_pull_db
    ;;
  all)
    do_push_data
    do_pull
    do_pull_db
    ;;
esac

echo "Done."
