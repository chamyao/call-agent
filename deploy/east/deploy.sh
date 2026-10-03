#!/usr/bin/env bash
# Deploys call-agent to db-east as the restricted `phonebot` user.
# App logs: /home/phonebot/app/logs/phonebot.log. Start/restart: sudo systemctl restart phonebot
# Needs a `Host east` entry in ~/.ssh/config (User phonebot, IdentityFile ~/.ssh/east_ed25519).
# Jerry's phonebot.service runs:
#   /home/phonebot/app/.venv/bin/python -m call_agent serve --host 127.0.0.1 --port 8010
# with WorkingDirectory=/home/phonebot/app. Restarts go through him (or `sudo systemctl restart phonebot`).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HOST="${EAST_HOST:-east}"
APP=/home/phonebot/app
cd "$REPO"

[ -f .env ] || { echo "No .env here to copy." >&2; exit 1; }
grep -q '^PUBLIC_URL=https://calls.ceruleantokyo.xyz$' .env \
  || { echo "Set PUBLIC_URL=https://calls.ceruleantokyo.xyz in .env first." >&2; exit 1; }

# Code only: no .env, venv, call logs or local config
rsync -az --delete \
  --exclude .git --exclude .venv --exclude .env --exclude profile.md \
  --exclude calls --exclude logs --exclude tasks --exclude __pycache__ --exclude .pytest_cache \
  --exclude deploy/mac/frpc.toml \
  ./ "$HOST:$APP/"

# Secrets go by file, never through the terminal
ssh "$HOST" "umask 077; mkdir -p $APP"
scp -q .env "$HOST:$APP/.env"
[ -f profile.md ] && scp -q profile.md "$HOST:$APP/profile.md"
ssh "$HOST" "chmod 600 $APP/.env $APP/profile.md 2>/dev/null; cd $APP && \
  { [ -x .venv/bin/python ] || python3 -m venv .venv; } && \
  .venv/bin/pip install -q -r requirements.txt && echo 'Installed in $APP'"

ssh "$HOST" "sudo systemctl restart phonebot" && sleep 3
echo "Check: curl -s https://calls.ceruleantokyo.xyz/health"
