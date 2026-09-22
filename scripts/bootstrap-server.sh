#!/bin/bash
# One-time setup for a fresh Ubuntu 24.04 droplet (see docs/DEPLOYMENT.md).
# Run as root on the droplet itself:
#   curl -fsSL https://raw.githubusercontent.com/yonixnetwork/Yonix-Alpha/<branch>/scripts/bootstrap-server.sh | bash
# or, having already cloned the repo some other way:
#   sudo bash scripts/bootstrap-server.sh
#
# Idempotent: re-running on a box that already has Docker/the repo/ufw/
# fail2ban/unattended-upgrades configured skips whatever's already done
# rather than failing or duplicating it. Installs Docker Engine + the
# compose plugin, clones (or updates) the repo into /opt/yonixalpha, opens
# only 22/80/443 in ufw, enables unattended security upgrades, and installs
# fail2ban's default SSH jail. It does NOT create .env, obtain TLS certs, or
# start any service — those are separate, deliberate steps in
# docs/DEPLOYMENT.md, not something a bootstrap script should do
# unattended with production credentials.
#
# Neither hardening addition below can lock out a legitimate operator:
# unattended-upgrades only ever installs *security* updates (never a
# release upgrade, never anything requiring a config-file decision it would
# have to guess at); fail2ban's sshd jail only ever bans an IP after
# repeated *failed password* attempts (a key-based login, which is what
# docs/DEPLOYMENT.md's own `ssh root@<droplet-ip>` assumes, never counts as
# a failure to trigger it), and even a false-positive ban is temporary
# (1 hour, see below) rather than permanent.
set -euo pipefail

REPO_URL="https://github.com/yonixnetwork/Yonix-Alpha.git"
REPO_DIR="/opt/yonixalpha"
REPO_BRANCH="${YONIXALPHA_DEPLOY_BRANCH:-main}"

if [ "$(id -u)" -ne 0 ]; then
    echo "bootstrap-server.sh must run as root (it installs system packages and edits the firewall)." >&2
    exit 1
fi

echo "==> Installing Docker Engine + compose plugin"
if ! command -v docker >/dev/null 2>&1; then
    apt-get update
    apt-get install -y ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    # shellcheck disable=SC1091
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
else
    echo "    docker already installed ($(docker --version)), skipping."
fi

echo "==> Cloning/updating the repository"
if [ -d "${REPO_DIR}/.git" ]; then
    git -C "${REPO_DIR}" fetch origin "${REPO_BRANCH}"
    git -C "${REPO_DIR}" checkout "${REPO_BRANCH}"
    git -C "${REPO_DIR}" pull --ff-only origin "${REPO_BRANCH}"
else
    git clone --branch "${REPO_BRANCH}" "${REPO_URL}" "${REPO_DIR}"
fi

echo "==> Configuring ufw (22/tcp, 80/tcp, 443/tcp only)"
if command -v ufw >/dev/null 2>&1; then
    ufw allow 22/tcp >/dev/null
    ufw allow 80/tcp >/dev/null
    ufw allow 443/tcp >/dev/null
    ufw --force enable
    ufw status verbose
else
    echo "    ufw not installed — skipping firewall setup. Configure the droplet's firewall manually (only 22/80/443 should be reachable)." >&2
fi

echo "==> Enabling unattended security upgrades"
if ! dpkg -s unattended-upgrades >/dev/null 2>&1; then
    apt-get update
    apt-get install -y unattended-upgrades
    dpkg-reconfigure -f noninteractive unattended-upgrades
else
    echo "    unattended-upgrades already installed, skipping."
fi

echo "==> Installing fail2ban (SSH jail only, 1h ban after 5 failed attempts in 10m)"
if ! command -v fail2ban-client >/dev/null 2>&1; then
    apt-get update
    apt-get install -y fail2ban
    cat > /etc/fail2ban/jail.local <<'JAIL'
[sshd]
enabled = true
maxretry = 5
findtime = 10m
bantime = 1h
JAIL
    systemctl enable --now fail2ban
else
    echo "    fail2ban already installed, skipping."
fi

cat <<EOF

==> Bootstrap complete. Next steps (see docs/DEPLOYMENT.md):
    1. cd ${REPO_DIR}
    2. cp .env.example .env && edit .env with real secrets
    3. docker compose -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml up -d --build
    4. Run the certbot TLS bootstrap command once DNS for your domain points at this server.
EOF
