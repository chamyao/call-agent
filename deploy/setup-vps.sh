#!/usr/bin/env bash
# Run on the VPS (public IP). Installs frps + Caddy so that
# https://<domain> reaches the call agent running on your home machine.
#
#   sudo bash deploy/setup-vps.sh calls.yourdomain.com
#
# Prints the frp token to use with setup-home.sh. Safe to re-run; it keeps
# the existing token.
set -euo pipefail

DOMAIN="${1:-}"
FRP_VERSION="${FRP_VERSION:-0.71.0}"
FRP_DIR=/etc/frp

if [[ -z "$DOMAIN" ]]; then
  echo "usage: sudo bash $0 calls.yourdomain.com" >&2
  exit 1
fi
if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/lib.sh"

install_frp "$FRP_VERSION" frps

# --- frps config -------------------------------------------------------------
mkdir -p "$FRP_DIR"
if [[ -f "$FRP_DIR/frps.toml" ]] && grep -q '^auth.token' "$FRP_DIR/frps.toml"; then
  FRP_TOKEN="$(sed -n 's/^auth.token = "\(.*\)"/\1/p' "$FRP_DIR/frps.toml")"
else
  FRP_TOKEN="$(openssl rand -hex 24)"
fi
cat > "$FRP_DIR/frps.toml" <<EOF
# Managed by call-agent/deploy/setup-vps.sh
bindPort = 7000
# Tunneled sites are served here; only Caddy (same machine) should reach it.
vhostHTTPPort = 8080
auth.method = "token"
auth.token = "$FRP_TOKEN"
EOF
chmod 600 "$FRP_DIR/frps.toml"

install -m 644 "$HERE/systemd/frps.service" /etc/systemd/system/frps.service
systemctl daemon-reload
systemctl enable --now frps
systemctl restart frps

# --- Caddy (automatic HTTPS) -------------------------------------------------
if ! command -v caddy >/dev/null; then
  apt-get update -y
  if ! apt-get install -y caddy; then
    apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
      | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
      > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -y
    apt-get install -y caddy
  fi
fi

CADDYFILE=/etc/caddy/Caddyfile
if ! grep -q "^$DOMAIN {" "$CADDYFILE" 2>/dev/null; then
  # Replace the stock welcome-page config, keep anything else you added.
  if grep -q '^:80 {' "$CADDYFILE" 2>/dev/null; then
    : > "$CADDYFILE"
  fi
  cat >> "$CADDYFILE" <<EOF

$DOMAIN {
	reverse_proxy 127.0.0.1:8080
}
EOF
fi
systemctl enable --now caddy
systemctl reload caddy || systemctl restart caddy

# --- firewall ----------------------------------------------------------------
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp
  ufw allow 443/tcp
  ufw allow 7000/tcp
  ufw deny 8080/tcp
fi

PUBLIC_IP="$(curl -fsS https://api.ipify.org || echo '<this server IP>')"
cat <<EOF

VPS is set up.

  1. DNS: make an A record  $DOMAIN -> $PUBLIC_IP  (if you haven't).
  2. If your cloud provider has its own firewall, allow TCP 80, 443, 7000
     and keep 8080 closed.
  3. On the home machine, run:

     bash deploy/setup-home.sh $PUBLIC_IP $DOMAIN $FRP_TOKEN

EOF
