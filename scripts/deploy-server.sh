#!/bin/bash
# Deploy the server to the LAN Ubuntu box as a systemd service (no Docker).
# Needs SSH key login. Usage: scripts/deploy-server.sh user@host [port]
# The host can also come from AH_DEPLOY_HOST, or from a one-line file named .deploy-host
# in the repo root (not committed), so your server's address stays out of the repository.
set -euo pipefail
cd "$(dirname "$0")/.."
HOST="${1:-${AH_DEPLOY_HOST:-$(cat .deploy-host 2>/dev/null || true)}}"
[ -n "$HOST" ] || { echo "usage: scripts/deploy-server.sh user@host [port]   (or put user@host in .deploy-host)"; exit 1; }
PORT="${2:-8080}"
DEST=/opt/adventure-hub
TOKEN=$(sed -n 's/^api_token *= *"\(.*\)"/\1/p' agent/config.toml)
[ -n "$TOKEN" ] && [ "$TOKEN" != "CHANGE_ME" ] || { echo "set api_token in agent/config.toml first"; exit 1; }

# Read-only token for the dashboard, kept in a local untracked file.
[ -s server/.dashboard-token ] || (umask 077; python3 -c 'import secrets;print(secrets.token_urlsafe(32))' > server/.dashboard-token)
READ_TOKEN=$(cat server/.dashboard-token)

ssh -o BatchMode=yes "$HOST" "mkdir -p $DEST/data"
rsync -az --delete --exclude .venv --exclude data --exclude .env --exclude client --exclude __pycache__ \
  server/ "$HOST:$DEST/"
# The client (addon, sync program, start scripts) for the setup page to hand out. Only source
# files are sent: the agent's own config.toml, which holds the token, never leaves this computer.
ssh -o BatchMode=yes "$HOST" "mkdir -p $DEST/client/adventuresync $DEST/client/addon"
rsync -az --delete --exclude __pycache__ --include '*/' --include '*.py' --include '*.png' --include '*.ico' --exclude '*' agent/adventuresync/ "$HOST:$DEST/client/adventuresync/"
rsync -az --delete addon/ "$HOST:$DEST/client/addon/"
rsync -az client/README.txt agent/requirements.txt agent/AdventureSync.pyw "$HOST:$DEST/client/"
# World database (static game data), if one has been built with `python -m world_importer`.
# Uploaded under a temporary name and swapped in, so the running server never sees half a file.
if [ -s worlddata/world.db ]; then
  rsync -az worlddata/world.db "$HOST:$DEST/data/world.db.new"
  ssh -o BatchMode=yes "$HOST" "mv $DEST/data/world.db.new $DEST/data/world.db"
fi
# The token travels over stdin so it never appears in a process list.
printf 'AH_API_TOKEN=%s\nAH_READ_TOKEN=%s\nAH_DB_PATH=%s/data/adventure.db\nAH_PORT=%s\n' "$TOKEN" "$READ_TOKEN" "$DEST" "$PORT" |
  ssh -o BatchMode=yes "$HOST" "umask 077; cat > $DEST/.env"
ssh -o BatchMode=yes "$HOST" bash -s <<REMOTE
set -euo pipefail
cd $DEST
if [ ! -x .venv/bin/pip ]; then
  rm -rf .venv
  if ! python3 -m venv .venv >/dev/null 2>&1; then
    rm -rf .venv
    # Unrelated broken apt repos must not stop us; the package index is usually good enough.
    apt-get update -qq >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv >/dev/null
    python3 -m venv .venv
  fi
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
# The service runs as its own unprivileged account, which owns nothing but the data folder.
id -u adventure-hub >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin adventure-hub
chown -R adventure-hub:adventure-hub data
cp deploy/adventure-hub.service /etc/systemd/system/adventure-hub.service
systemctl daemon-reload
systemctl enable adventure-hub >/dev/null 2>&1
systemctl restart adventure-hub
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then ufw allow $PORT/tcp >/dev/null; fi
sleep 2
systemctl is-active adventure-hub
curl -fsS http://127.0.0.1:$PORT/api/v1/health; echo
REMOTE
echo "deployed: http://${HOST#*@}:$PORT/api/v1/health"
# One-click dashboard link; the token is in the URL fragment, which is never sent to the server.
(umask 077; echo "http://${HOST#*@}:$PORT/#token=$READ_TOKEN" > dashboard-link.txt)
echo "dashboard: http://${HOST#*@}:$PORT/  (first-time link with token saved in dashboard-link.txt)"
