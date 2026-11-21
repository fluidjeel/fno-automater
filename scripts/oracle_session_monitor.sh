#!/usr/bin/env bash
# Log paper+M2 health until NSE cash close (15:35 IST). Run on Oracle in background.
set -euo pipefail
REPO="${1:-$HOME/fno-automated}"
LOG="$REPO/data/paper/session_monitor.log"
while true; do
  TS="$(TZ=Asia/Kolkata date '+%Y-%m-%dT%H:%M:%S%z')"
  HB="missing"
  if test -f "$REPO/data/paper/session_heartbeat.json"; then
    HB="$(python3 -c "import json; d=json.load(open('$REPO/data/paper/session_heartbeat.json')); print(d.get('cycle_count'), d.get('open_positions'), d.get('had_requests'))" 2>/dev/null || echo bad)"
  fi
  TICK="$(wc -c "$REPO/data/m2_agentic/ticks/$(TZ=Asia/Kolkata date +%F)/.tick_store" 2>/dev/null | awk '{print $1}' || echo 0)"
  SVC="$(systemctl is-active fno-paper-session fno-m2-agentic-tick 2>/dev/null | tr '\n' '/' | sed 's|/$||')"
  echo "$TS cycles/open/req=$HB tick_marker=$TICK svc=$SVC" >>"$LOG"
  NOW_IST="$(TZ=Asia/Kolkata date +%H:%M)"
  if test "$NOW_IST" \> "15:35" || test "$NOW_IST" = "15:35"; then
    echo "$TS monitor stop (past 15:35 IST)" >>"$LOG"
    exit 0
  fi
  sleep 300
done