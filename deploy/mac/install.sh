#!/usr/bin/env bash
# Runs the bot and the frp tunnel on this Mac in the background, starting at login.
# Re-run after changing .env. Uninstall with: deploy/mac/install.sh --uninstall
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/call-agent"
LABELS=(com.call-agent.server com.call-agent.frpc)

unload() {
  for label in "${LABELS[@]}"; do
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
  done
}

if [ "${1:-}" = "--uninstall" ]; then
  unload
  echo "Stopped and removed the call-agent background services."
  exit 0
fi

cd "$REPO"
[ -f .env ] || { echo "No .env in $REPO. Copy .env.example to .env and fill it in." >&2; exit 1; }
[ -x .venv/bin/python ] || { echo "No .venv. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2; exit 1; }
FRPC="$(command -v frpc || true)"
[ -n "$FRPC" ] || { echo "frpc not found. Install it with: brew install frpc" >&2; exit 1; }

# Check .env and write frpc.toml from it; prints PUBLIC_URL
PUBLIC_URL="$(.venv/bin/python - <<'PY'
import string, sys
from dotenv import dotenv_values

env = {k: (v or "").strip() for k, v in dotenv_values(".env").items()}
needed = ["ANTHROPIC_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
          "PUBLIC_URL", "CALL_AGENT_TOKEN", "OWNER_NAME", "FRP_SERVER_ADDR", "FRP_TOKEN"]
missing = [k for k in needed if not env.get(k) or env[k].endswith("...") or env[k] == "change-me"]
if missing:
    sys.exit("Fill these in .env first: " + " ".join(missing))
env["PUBLIC_DOMAIN"] = env["PUBLIC_URL"].split("://", 1)[-1].split("/")[0]
src = open("deploy/mac/frpc.toml.template").read()
open("deploy/mac/frpc.toml", "w").write(string.Template(src).substitute(env))
print(env["PUBLIC_URL"].rstrip("/"))
PY
)"
chmod 600 deploy/mac/frpc.toml
"$FRPC" verify -c deploy/mac/frpc.toml

mkdir -p "$AGENTS" "$LOGS"
unload

write_plist() {  # label, then program arguments
  local label="$1"; shift
  {
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>ProgramArguments</key>
  <array>
PLIST
    for arg in "$@"; do printf '    <string>%s</string>\n' "$arg"; done
    cat <<PLIST
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOGS/$label.log</string>
  <key>StandardErrorPath</key><string>$LOGS/$label.log</string>
</dict>
</plist>
PLIST
  } > "$AGENTS/$label.plist"
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
}

write_plist com.call-agent.server "$REPO/.venv/bin/python" -m call_agent serve
write_plist com.call-agent.frpc "$FRPC" -c "$REPO/deploy/mac/frpc.toml"

echo "Started. Logs: $LOGS"
sleep 4
if curl -fsS "$PUBLIC_URL/health" >/dev/null; then
  echo "OK: $PUBLIC_URL/health is reachable from the internet."
else
  echo "Not reachable yet at $PUBLIC_URL/health. Check the logs above, and that the VPS setup finished."
fi
