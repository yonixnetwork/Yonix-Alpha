#!/usr/bin/env bash
# Installs the dashboard key updater (Settings -> Providers -> Change keys).
#
# What it sets up, on this server:
#   runtime/env-requests/            spool directory, writable only by the api
#                                    container's user (mode 700)
#   yonixalpha-env-updater.path      systemd unit: fires when a request appears
#   yonixalpha-env-updater.service   runs scripts/apply-env-requests.py as root:
#                                    validates, backs up .env, merges the new
#                                    values, `docker compose up -d`
#
# The api container never reads .env and never gets the Docker socket. Only
# provider API keys/URLs are accepted (see packages/core-py/yonixalpha_core/
# env_updates.py); wallet keys, passwords, testnet flags and the trading
# locks stay server-only (scripts/set-keys.sh).
#
# Usage (as root, from the repository root):
#   scripts/install-env-updater.sh              install / reinstall
#   scripts/install-env-updater.sh --uninstall  remove the units (spool kept)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"
COMPOSE="docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml"
SPOOL="${REPO_ROOT}/runtime/env-requests"
UNIT_DIR=/etc/systemd/system
NAME=yonixalpha-env-updater

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root." >&2
    exit 1
fi

if [ "${1:-}" = "--uninstall" ]; then
    systemctl disable --now "${NAME}.path" 2>/dev/null || true
    rm -f "${UNIT_DIR}/${NAME}.path" "${UNIT_DIR}/${NAME}.service"
    systemctl daemon-reload
    echo "Removed ${NAME}. Dashboard key changes are now refused (the spool ${SPOOL} was kept)."
    exit 0
fi

[ -f .env ] || { echo "No .env in ${REPO_ROOT}." >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required." >&2; exit 1; }

# The api container's user id: it must own the spool to write requests and
# read results.
API_UID=$(${COMPOSE} run --rm --no-deps -T --entrypoint id api -u 2>/dev/null | tr -dc '0-9' || true)
API_UID=${API_UID:-1000}
echo "api container user id: ${API_UID}"

mkdir -p "${SPOOL}"
chown "${API_UID}:${API_UID}" "${SPOOL}"
chmod 700 "${SPOOL}"
chmod 600 .env

cat > "${UNIT_DIR}/${NAME}.service" <<EOF
[Unit]
Description=YonixAlpha: apply API-key changes queued from the dashboard
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=${REPO_ROOT}
Environment=ENV_SPOOL=${SPOOL}
Environment=ENV_SPOOL_UID=${API_UID}
ExecStart=/usr/bin/python3 ${REPO_ROOT}/scripts/apply-env-requests.py
NoNewPrivileges=yes
PrivateTmp=yes
UMask=0077
EOF

cat > "${UNIT_DIR}/${NAME}.path" <<EOF
[Unit]
Description=YonixAlpha: watch for dashboard API-key changes

[Path]
PathExistsGlob=${SPOOL}/req-*.json
Unit=${NAME}.service

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now "${NAME}.path"

# The api needs the spool mounted (docker-compose.prod.yml); recreate it.
${COMPOSE} up -d api

echo
echo "Installed. Status:   systemctl status ${NAME}.path"
echo "Applied changes log: journalctl -u ${NAME}.service"
echo ".env backups:        /opt/yonixalpha-backups/env/"
echo "Remove:              scripts/install-env-updater.sh --uninstall"
