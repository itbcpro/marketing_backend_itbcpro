#!/usr/bin/env bash
# One-time, additive server preparation. Run as root:  sudo bash setup-server.sh <ci-public-key-file>
# It installs Docker from the official repository if missing and creates a locked-down deploy user.
# It does not touch nginx, existing services, firewall rules or other users.
set -Eeuo pipefail

PUBKEY_FILE="${1:?usage: setup-server.sh <ci-public-key-file>}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
ROOT="${DEPLOY_ROOT:-/opt/itbcpro}"
HERE="$(cd "$(dirname "$0")" && pwd)"

if ! command -v docker >/dev/null; then
  . /etc/os-release
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
fi
systemctl enable --now docker

id "$DEPLOY_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$DEPLOY_USER"
usermod -aG docker "$DEPLOY_USER"

install -d -m 750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$ROOT" "$ROOT/bin"
install -m 750 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$HERE/deploy.sh" "$ROOT/bin/deploy.sh"

# The CI key can only run the deploy script: no shell, no forwarding, no pty.
ssh_dir="/home/$DEPLOY_USER/.ssh"
install -d -m 700 -o "$DEPLOY_USER" -g "$DEPLOY_USER" "$ssh_dir"
key="$(cat "$PUBKEY_FILE")"
line="command=\"$ROOT/bin/deploy.sh\",restrict $key"
grep -qxF "$line" "$ssh_dir/authorized_keys" 2>/dev/null || echo "$line" >> "$ssh_dir/authorized_keys"
chown "$DEPLOY_USER:$DEPLOY_USER" "$ssh_dir/authorized_keys"
chmod 600 "$ssh_dir/authorized_keys"

echo "ready: docker $(docker --version), deploy user '$DEPLOY_USER', root '$ROOT'"
