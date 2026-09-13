#!/usr/bin/env bash
# Sync fno-automated to the Oracle Ubuntu VM, install with uv, run tests, optionally
# start the data pipeline.
#
# SSH key (do not copy into this repo — stays in blue-green):
#   ../blue-green/keys/ssh-key-2026-09-12.key
#
# Usage:
#   ./deploy/deploy_oracle.sh --host ubuntu@92.4.94.79 \
#     --key ../blue-green/keys/ssh-key-2026-09-12.key
#
#   ./deploy/deploy_oracle.sh --host ubuntu@92.4.94.79 --key ... --with-secrets --fetch
#
# Options:
#   --host HOST       user@IP (required)
#   --key PATH        SSH private key (default: ../blue-green/keys/ssh-key-2026-09-12.key)
#   --dir PATH        remote directory (default: fno-automated)
#   --user NAME       remote user when host is a bare IP (default: ubuntu)
#   --with-secrets    sync .env and .fyers_token for live Fyers calls
#   --env-file PATH   secrets file to upload as .env (default: ./.env)
#   --fetch           run one data pipeline fetch after deploy
#   --install         install systemd timer on the VM
#   --tar             tar-over-ssh instead of rsync
#
# Env fallbacks: VM_HOST, SSH_KEY, REMOTE_DIR, REMOTE_USER
set -euo pipefail

VM_HOST="${VM_HOST:-}"
SSH_KEY="${SSH_KEY:-}"
REMOTE_DIR="${REMOTE_DIR:-fno-automated}"
REMOTE_USER="${REMOTE_USER:-ubuntu}"
WITH_SECRETS=0
RUN_FETCH=0
RUN_INSTALL=0
USE_TAR=0
ENV_FILE=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_KEY="$REPO_ROOT/../blue-green/keys/ssh-key-2026-09-12.key"

usage() { sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --host) VM_HOST="$2"; shift 2 ;;
    --key) SSH_KEY="$2"; shift 2 ;;
    --dir) REMOTE_DIR="$2"; shift 2 ;;
    --user) REMOTE_USER="$2"; shift 2 ;;
    --with-secrets) WITH_SECRETS=1; shift ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    --fetch) RUN_FETCH=1; shift ;;
    --install) RUN_INSTALL=1; shift ;;
    --tar) USE_TAR=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown arg: $1"; usage 1 ;;
  esac
done

[ -z "$VM_HOST" ] && { echo "ERROR: pass --host <user@IP>"; usage 1; }
[ -z "$SSH_KEY" ] && SSH_KEY="$DEFAULT_KEY"

case "$VM_HOST" in
  *@*) TARGET="$VM_HOST" ;;
  *) TARGET="$REMOTE_USER@$VM_HOST" ;;
esac

if [ ! -f "$SSH_KEY" ] && [ -f "$OLDPWD/$SSH_KEY" ]; then
  SSH_KEY="$OLDPWD/$SSH_KEY"
fi

command -v ssh >/dev/null || { echo "ERROR: ssh not found"; exit 1; }
[ -f "$SSH_KEY" ] || { echo "ERROR: key not found: $SSH_KEY"; exit 1; }
chmod 600 "$SSH_KEY" 2>/dev/null || true

if [ -z "$ENV_FILE" ]; then
  ENV_FILE="$REPO_ROOT/.env"
elif [ ! -f "$ENV_FILE" ] && [ -f "$OLDPWD/$ENV_FILE" ]; then
  ENV_FILE="$OLDPWD/$ENV_FILE"
fi

if [ "$WITH_SECRETS" -eq 1 ] || [ "$RUN_FETCH" -eq 1 ]; then
  if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: Fyers credentials required but not found: $ENV_FILE"
    echo "  Copy .env.example to .env and set FYERS_APP_ID / FYERS_SECRET_KEY,"
    echo "  or pass --env-file /path/to/your.env"
    exit 1
  fi
fi

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15
          -o ServerAliveInterval=30 -i "$SSH_KEY")

# '/data/' is anchored to the repo root so src/trading/data/ still syncs.
FILTERS=(
  '.venv/' '__pycache__/' '*.pyc' '.pytest_cache/' '.mypy_cache/' '.ruff_cache/'
  '.git/' '.DS_Store' '/data/' 'audit/' '*.duckdb' '*.parquet'
  'keys/' '*.key' '*.pem'
)
[ "$WITH_SECRETS" -eq 0 ] && FILTERS+=('.env' '.fyers_token' '.fyers_refresh_token')

echo "==> uploading to $TARGET:$REMOTE_DIR"
if [ "$USE_TAR" -eq 1 ]; then
  TAR_EXCLUDES=(); for f in "${FILTERS[@]}"; do
    case "$f" in /*) TAR_EXCLUDES+=(--exclude ".$f") ;; *) TAR_EXCLUDES+=(--exclude "./$f") ;; esac
  done
  tar czf - -C "$REPO_ROOT" "${TAR_EXCLUDES[@]}" . | ssh "${SSH_OPTS[@]}" "$TARGET" \
    "mkdir -p '$REMOTE_DIR' && tar xzf - -C '$REMOTE_DIR'"
else
  command -v rsync >/dev/null || { echo "ERROR: rsync not found (use --tar)"; exit 1; }
  RSYNC_EXCLUDES=(); for f in "${FILTERS[@]}"; do RSYNC_EXCLUDES+=(--exclude "$f"); done
  rsync -a -e "ssh ${SSH_OPTS[*]}" "${RSYNC_EXCLUDES[@]}" "$REPO_ROOT/" "$TARGET:$REMOTE_DIR/"
fi

if [ "$WITH_SECRETS" -eq 1 ]; then
  echo "==> uploading secrets to $TARGET:$REMOTE_DIR/.env"
  rsync -a -e "ssh ${SSH_OPTS[*]}" "$ENV_FILE" "$TARGET:$REMOTE_DIR/.env"
  if [ -f "$REPO_ROOT/.fyers_token" ]; then
    rsync -a -e "ssh ${SSH_OPTS[*]}" "$REPO_ROOT/.fyers_token" "$TARGET:$REMOTE_DIR/.fyers_token"
  fi
  if [ -f "$REPO_ROOT/.fyers_refresh_token" ]; then
    rsync -a -e "ssh ${SSH_OPTS[*]}" "$REPO_ROOT/.fyers_refresh_token" "$TARGET:$REMOTE_DIR/.fyers_refresh_token"
  fi
fi

echo "==> bootstrapping on VM"
ssh "${SSH_OPTS[@]}" "$TARGET" \
  "export REMOTE_DIR='$REMOTE_DIR'; export WITH_SECRETS='$WITH_SECRETS'; \
   export RUN_FETCH='$RUN_FETCH'; export RUN_INSTALL='$RUN_INSTALL'; bash -s" <<'REMOTE'
set -euo pipefail
cd "$HOME/$REMOTE_DIR"

# Ubuntu 24.04 ships 3.12 only; project pins 3.11. uv downloads it — no apt package.
sudo apt-get update -qq
sudo apt-get install -y -qq curl ca-certificates

if ! command -v uv >/dev/null 2>&1; then
  curl -fsSL https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

uv python install 3.11
uv sync --extra data
uv run ruff check .
uv run mypy
uv run pytest

if [ "$RUN_INSTALL" = "1" ]; then
  sudo ./deploy/install.sh
fi

if [ "$RUN_FETCH" = "1" ]; then
  if [ ! -f .env ]; then
    echo "ERROR: .env missing on VM; re-run deploy with --with-secrets" >&2
    exit 1
  fi
  uv run trading data fetch
fi

echo "Deploy complete on $(hostname)"
REMOTE

echo "✅ Done."
