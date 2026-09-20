#!/usr/bin/env bash
# Ask the Layer-4 weekly agent on the Oracle VM and print its recommendation.
# Run from your Mac (laptop), not on the VM:
#   ./scripts/oracle_agent_ask.sh
#   ./scripts/oracle_agent_ask.sh --sync          # rsync code+.env first
#   ./scripts/oracle_agent_ask.sh --cohort PATH   # remote cohort JSON
#   ./scripts/oracle_agent_ask.sh --allow-fixture # demo fixture cohort only
#
# Does not print secrets. Requires DEEPSEEK_API_KEY in the VM's ~/fno-automated/.env

set -euo pipefail

HOST="${VM_HOST:-ubuntu@92.4.94.79}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEY="${SSH_KEY:-$REPO_ROOT/../blue-green/keys/ssh-key-2026-09-12.key}"
REMOTE_DIR="${REMOTE_DIR:-fno-automated}"
SYNC=0
COHORT=""
ALLOW_FIXTURE=0
HISTORY_DAYS="${HISTORY_DAYS:-20}"
SYMBOL="${SYMBOL:-NSE:NIFTY50-INDEX}"
PULL=1

usage() {
  sed -n '2,14p' "$0"
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sync) SYNC=1; shift ;;
    --pull) PULL=1; shift ;;
    --no-pull) PULL=0; shift ;;
    --host) HOST="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --cohort) COHORT="$2"; shift 2 ;;
    --allow-fixture) ALLOW_FIXTURE=1; shift ;;
    --symbol) SYMBOL="$2"; shift 2 ;;
    --history-days) HISTORY_DAYS="$2"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) echo "unknown arg: $1" >&2; usage 1 ;;
  esac
done

[[ -f "$KEY" ]] || { echo "ERROR: SSH key not found: $KEY" >&2; exit 1; }
chmod 600 "$KEY" 2>/dev/null || true

SSH=(ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20
     -o ServerAliveInterval=30 -i "$KEY" "$HOST")

if [[ "$SYNC" -eq 1 ]]; then
  echo "==> syncing repo + .env to $HOST:$REMOTE_DIR"
  "$REPO_ROOT/deploy/deploy_oracle.sh" --host "$HOST" --key "$KEY" --with-secrets
fi

echo "==> asking weekly agent on $HOST (DeepSeek, --enable; shipped agent.yaml stays disabled)"
# shellcheck disable=SC2029
"${SSH[@]}" "REMOTE_DIR=$REMOTE_DIR COHORT=$COHORT ALLOW_FIXTURE=$ALLOW_FIXTURE SYMBOL=$SYMBOL HISTORY_DAYS=$HISTORY_DAYS bash -s" <<'REMOTE'
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/${REMOTE_DIR}"

if [[ ! -f .env ]]; then
  echo "ERROR: ~/${REMOTE_DIR}/.env missing (run with --sync once)" >&2
  exit 2
fi
if ! grep -q '^DEEPSEEK_API_KEY=.\+' .env; then
  echo "ERROR: DEEPSEEK_API_KEY not set in VM .env" >&2
  exit 2
fi

mkdir -p data/paper/agent_runs
OUT="data/paper/agent_runs"
ARGS=(agent weekly --enable --symbol "$SYMBOL" --history-days "$HISTORY_DAYS" --out-dir "$OUT")
if [[ -n "${COHORT}" ]]; then
  ARGS+=("$COHORT")
elif [[ "${ALLOW_FIXTURE}" -eq 0 ]]; then
  LATEST_COHORT=$(ls -t data/paper/cohorts/*.json 2>/dev/null | head -1 || true)
  if [[ -n "${LATEST_COHORT}" ]]; then
    echo "==> using latest paper cohort on VM: $LATEST_COHORT"
    ARGS+=("$LATEST_COHORT")
  fi
fi
if [[ "${ALLOW_FIXTURE}" -eq 1 ]]; then
  ARGS+=(--allow-fixture)
fi

set +e
uv run trading "${ARGS[@]}" 2>"$OUT/_last_ask.err" | tee "$OUT/_last_ask.out"
EC=$?
set -e
if [[ $EC -ne 0 ]]; then
  echo "ERROR: agent exited $EC" >&2
  tail -40 "$OUT/_last_ask.err" >&2 || true
  exit $EC
fi

LATEST=$(ls -td "$OUT"/*/ 2>/dev/null | head -1 || true)
if [[ -z "${LATEST:-}" ]]; then
  echo "ERROR: no agent_runs directory created" >&2
  exit 3
fi

python3 - "$LATEST" <<'PY'
import json, sys
from pathlib import Path
run = Path(sys.argv[1])
proposal = json.loads((run / "proposal.json").read_text())
meta = {}
mp = run / "meta.json"
if mp.exists():
    meta = json.loads(mp.read_text())
attn = []
ap = run / "attention.json"
if ap.exists():
    attn = json.loads(ap.read_text())

rec = proposal.get("recommendation") or proposal.get("stance")
conf = proposal.get("confidence")
fam = proposal.get("family_actions") or proposal.get("family_actions") or []
# tolerate schema variants
if not fam:
    fam = proposal.get("family_actions") or proposal.get("actions") or []
narrative = proposal.get("narrative") or proposal.get("reasoning") or ""
pid = proposal.get("proposal_id") or proposal.get("id") or run.name

cohort_label = meta.get("cohort_source") or meta.get("cohort") or "none"

print()
print("========== ORACLE AGENT RESPONSE ==========")
print(f"run:          {run}")
print(f"proposal_id:  {pid}")
print(f"model:        {meta.get('model') or (proposal.get('versions') or {}).get('model')}")
print(f"recommendation: {rec}")
print(f"confidence:   {conf}")
print(f"cohort:       {cohort_label}")
print(f"symbol:       {meta.get('symbol', '')}")
print("--- family_actions ---")
if isinstance(fam, list) and fam:
    for a in fam:
        if isinstance(a, dict):
            print(f"  {a.get('strategy_id') or a.get('family')}: {a.get('stance') or a.get('action')}")
        else:
            print(f"  {a}")
else:
    print("  (none)")
if attn:
    print("--- attention ---")
    for a in attn:
        print(f"  {a.get('blocker') or a.get('code')}: {(a.get('detail') or a.get('message') or '')[:200]}")
print("--- narrative (truncated) ---")
print((narrative or "")[:1200])
print("===========================================")
print(f"full proposal: {run / 'proposal.json'}")
print(f"reasoning:     {run / 'reasoning.md'}")
PY
REMOTE

if [[ "$PULL" -eq 1 ]]; then
  echo "==> pulling agent run artifacts from $HOST:$REMOTE_DIR/$OUT to $REPO_ROOT/$OUT"
  mkdir -p "$REPO_ROOT/$OUT"
  rsync -avz -e "ssh -o StrictHostKeyChecking=accept-new -i $KEY" "$HOST:$REMOTE_DIR/$OUT/" "$REPO_ROOT/$OUT/"
  echo "✅ run artifacts synchronized to local $REPO_ROOT/$OUT"
fi
