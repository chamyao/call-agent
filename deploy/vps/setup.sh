#!/usr/bin/env bash
# Sets up a Debian/Ubuntu VPS as the public front door for call-agent:
#   Twilio --HTTPS--> Caddy (:443) --> frps (:8080) <--tunnel (:7000)-- frpc on your Mac --> bot (:8000)
#
# Run as root on the VPS:
#   sudo bash setup.sh calls.yourdomain.com <FRP_TOKEN>
# FRP_TOKEN must match FRP_TOKEN in the Mac's .env.
set -euo pipefail

DOMAIN="${1:?usage: setup.sh <domain> <frp-token>}"
TOKEN="${2:?usage: setup.sh <domain> <frp-token>}"
FRP_VERSION="0.71.0"

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }

case "$(uname -m)" in
  x86_64) ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "Unsupported CPU: $(uname -m)" >&2; exit 1 ;;
esac

apt-get update
apt-get install -y curl debian-keyring debian-archive-keyring apt-transport-https gnupg ufw

# --- frps -----------------------------------------------------------------
cd /tmp
curl -fsSL -o frp.tgz "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/frp_${FRP_VERSION}_linux_${ARCH}.tar.gz"
tar xzf frp.tgz
install -m 755 "frp_${FRP_VERSION}_linux_${ARCH}/frps" /usr/local/bin/frps
rm -rf frp.tgz "frp_${FRP_VERSION}_linux_${ARCH}"

mkdir -p /etc/frp
cat > /etc/frp/frps.toml <<CONF
bindPort = 7000
# Tunneled HTTP sites; only Caddy talks to this (the firewall keeps it private)
vhostHTTPPort = 8080
auth.method = "token"
auth.token = "${TOKEN}"
CONF
chmod 600 /etc/frp/frps.toml

cat > /etc/systemd/system/frps.service <<'UNIT'
[Unit]
Description=frp server
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/local/bin/frps -c /etc/frp/frps.toml
Restart=always
RestartSec=5
DynamicUser=yes

[Install]
WantedBy=multi-user.target
UNIT

# --- Caddy (gets the HTTPS certificate automatically) ----------------------
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi

cat > /etc/caddy/Caddyfile <<CONF
${DOMAIN} {
    reverse_proxy 127.0.0.1:8080
}
CONF

# --- Firewall --------------------------------------------------------------
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 7000/tcp
ufw --force enable

systemctl daemon-reload
systemctl enable --now frps
systemctl restart caddy

echo
echo "Done. frps is listening on :7000 and Caddy serves https://${DOMAIN}"
echo "Once the Mac side is running, check: curl https://${DOMAIN}/health"
