#!/usr/bin/env bash
# Ask the Layer-4 advise desk on the Oracle VM and print StructureAdvice.
# Run from your Mac:
#   ./scripts/oracle_agent_advise.sh
#   ./scripts/oracle_agent_advise.sh --sync
#   ./scripts/oracle_agent_advise.sh --cohort PATH
#   ./scripts/oracle_agent_advise.sh --allow-fixture
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

usage() {
  sed -n '2,12p' "$0"
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sync) SYNC=1; shift ;;
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

echo "==> asking advise desk on $HOST (DeepSeek, --enable; shipped agent.yaml stays disabled)"
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
ARGS=(agent advise --enable --symbol "$SYMBOL" --history-days "$HISTORY_DAYS" --out-dir "$OUT")
if [[ -n "${COHORT}" ]]; then
  ARGS+=("$COHORT")
fi
if [[ "${ALLOW_FIXTURE}" -eq 1 ]]; then
  ARGS+=(--allow-fixture)
fi

set +e
uv run trading "${ARGS[@]}" 2>"$OUT/_last_advise.err" | tee "$OUT/_last_advise.out"
RC=$?
set -e
if [[ $RC -ne 0 ]]; then
  echo "advise failed rc=$RC" >&2
  tail -n 40 "$OUT/_last_advise.err" >&2 || true
  exit $RC
fi
python3 - <<'PY'
import json, pathlib, re
text = pathlib.Path("data/paper/agent_runs/_last_advise.out").read_text()
# find last JSON object
start = text.rfind("{")
# prefer advice.json path if printed
m = re.search(r"advice:\s+(\S+)", text)
if m:
    path = pathlib.Path(m.group(1))
    data = json.loads(path.read_text())
else:
    data = json.loads(text[start:])
print(f"preferred: {data.get('preferred_structure')} stance={data.get('stance')} conf={data.get('confidence')}")
PY
REMOTE
