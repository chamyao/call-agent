#!/usr/bin/env bash
# Run on the machine that runs the call agent (e.g. your home server), as
# your normal user (it uses sudo where needed). Arguments come from the end
# of setup-vps.sh's output:
#
#   bash deploy/setup-home.sh <VPS_IP> <DOMAIN> <FRP_TOKEN>
#
# It installs the bot in a virtualenv, asks for your keys (typed into the
# terminal, never shown), connects the frp tunnel, and starts everything as
# services that come back after a reboot. Safe to re-run.
set -euo pipefail

VPS_IP="${1:-}"; DOMAIN="${2:-}"; FRP_TOKEN="${3:-}"
FRP_VERSION="${FRP_VERSION:-0.71.0}"
if [[ -z "$VPS_IP" || -z "$DOMAIN" || -z "$FRP_TOKEN" ]]; then
  echo "usage: bash $0 <VPS_IP> <DOMAIN> <FRP_TOKEN>" >&2
  exit 1
fi
if [[ $EUID -eq 0 ]]; then
  echo "Run as your normal user, not root (the script uses sudo itself)." >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(dirname "$HERE")"
source "$HERE/lib.sh"
cd "$REPO"

# --- Python environment --------------------------------------------------------
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo "Need Python 3.10 or newer." >&2
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
  if ! python3 -m venv .venv 2>/dev/null; then
    rm -rf .venv
    sudo apt-get update -y && sudo apt-get install -y python3-venv
    python3 -m venv .venv
  fi
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# --- .env ------------------------------------------------------------------------
[[ -f .env ]] || cp .env.example .env
chmod 600 .env

env_get() { .venv/bin/python - "$1" <<'PY'
import sys
from dotenv import dotenv_values
print(dotenv_values(".env").get(sys.argv[1]) or "")
PY
}
env_set() { .venv/bin/python - "$1" "$2" <<'PY'
import sys
from dotenv import set_key
set_key(".env", sys.argv[1], sys.argv[2])
PY
}
is_placeholder() { [[ -z "$1" || "$1" == *"..."* || "$1" == "change-me" || "$1" == *"your-subdomain"* || "$1" == "Your Name" || "$1" == "+15551234567" || "$1" == "+15559876543" ]]; }

ask() {  # ask KEY "prompt" [secret]
  local key="$1" prompt="$2" secret="${3:-}" current value
  current="$(env_get "$key")"
  if ! is_placeholder "$current"; then return; fi
  while true; do
    if [[ -n "$secret" ]]; then
      read -r -s -p "$prompt: " value; echo
    else
      read -r -p "$prompt: " value
    fi
    [[ -n "$value" ]] && break
  done
  env_set "$key" "$value"
}

echo "Enter your settings (saved only in $REPO/.env)."
ask ANTHROPIC_API_KEY  "Anthropic API key (sk-ant-api03-...)" secret
ask TWILIO_ACCOUNT_SID "Twilio Account SID (AC...)"
ask TWILIO_AUTH_TOKEN  "Twilio Auth Token" secret
ask TWILIO_FROM_NUMBER "Your Twilio phone number, like +15513070647"
ask OWNER_NAME         "Your name (the agent says it's calling for you)"
ask OWNER_PHONE        "Your cell for transfers, like +12054136659"
env_set PUBLIC_URL "https://$DOMAIN"
if is_placeholder "$(env_get CALL_AGENT_TOKEN)"; then
  env_set CALL_AGENT_TOKEN "$(openssl rand -hex 24)"
fi
[[ -f profile.md ]] || cp examples/profile.example.md profile.md

# --- frp client --------------------------------------------------------------------
sudo bash -c "$(declare -f install_frp); install_frp '$FRP_VERSION' frpc"
sudo mkdir -p /etc/frp
sudo tee /etc/frp/frpc.toml >/dev/null <<EOF
# Managed by call-agent/deploy/setup-home.sh
serverAddr = "$VPS_IP"
serverPort = 7000
auth.method = "token"
auth.token = "$FRP_TOKEN"

[[proxies]]
name = "call-agent"
type = "http"
localIP = "127.0.0.1"
localPort = 8000
customDomains = ["$DOMAIN"]
EOF
sudo chmod 600 /etc/frp/frpc.toml
sudo install -m 644 "$HERE/systemd/frpc.service" /etc/systemd/system/frpc.service

# --- call agent service ------------------------------------------------------------
sed -e "s|@USER@|$USER|g" -e "s|@REPO@|$REPO|g" "$HERE/systemd/call-agent.service" \
  | sudo tee /etc/systemd/system/call-agent.service >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now frpc call-agent
sudo systemctl restart frpc call-agent

# --- check -------------------------------------------------------------------------
echo "Checking https://$DOMAIN/health ..."
ok=""
for _ in $(seq 1 20); do
  if curl -fsS "https://$DOMAIN/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 3
done
if [[ -n "$ok" ]]; then
  echo "The bot is reachable at https://$DOMAIN"
else
  echo "Couldn't reach https://$DOMAIN/health yet. Check: DNS record, VPS firewall,"
  echo "  journalctl -u frpc -n 50   and   journalctl -u call-agent -n 50"
fi

cat <<EOF

Done. To let Claude place calls for you, add these to your Claude Code cloud
environment (environment menu -> Edit):
  Environment variables:
    CALL_AGENT_URL=https://$DOMAIN
    CALL_AGENT_TOKEN=$(env_get CALL_AGENT_TOKEN)
  Network access: Custom, add  $DOMAIN  to allowed domains.

Edit $REPO/profile.md with the details the agent may share on calls.
Test call from this machine:
  .venv/bin/python -m call_agent call --to +1YOURCELL --task "Test call: chat briefly, then say goodbye."
EOF
