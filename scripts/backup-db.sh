#!/bin/bash
# Dumps the running Postgres database to a timestamped, gzipped file and
# prunes anything older than the retention window. Run from the repo root
# on the server, e.g. via a daily cron entry (see docs/DEPLOYMENT.md):
#   0 3 * * * /opt/yonixalpha/scripts/backup-db.sh >> /var/log/yonixalpha-backup.log 2>&1
#
# Dumps via `docker compose exec` into the running postgres container and
# reads POSTGRES_USER/POSTGRES_DB from *that container's own environment*
# (already populated by its `env_file: ../../.env`) rather than parsing
# .env on the host — ADMIN_PASSWORD_HASH and other values in .env contain
# literal `$` characters that a naive `source .env` would try to expand
# as shell variables (a real bug this project hit once already, in the
# argon2 hash used to seed the admin account — see docs/DEPLOYMENT.md).
set -euo pipefail

COMPOSE="docker compose -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

BACKUP_DIR="${YONIXALPHA_BACKUP_DIR:-/opt/yonixalpha-backups}"
RETENTION_DAYS="${YONIXALPHA_BACKUP_RETENTION_DAYS:-14}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

if [ ! -f .env ]; then
    echo "No .env in ${REPO_ROOT} — is the stack even deployed here?" >&2
    exit 1
fi

mkdir -p "${BACKUP_DIR}"
OUT_FILE="${BACKUP_DIR}/yonixalpha-${TIMESTAMP}.sql.gz"

echo "==> Dumping database to ${OUT_FILE}"
# shellcheck disable=SC2016 # deliberately single-quoted: $POSTGRES_USER/$POSTGRES_DB
# must expand inside the container's own shell (populated by its env_file),
# never on this host — see the file header comment and docs/SECURITY.md
# section 5 for why .env is never sourced here.
${COMPOSE} exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "${OUT_FILE}"
echo "    $(du -h "${OUT_FILE}" | cut -f1) written."

echo "==> Pruning backups older than ${RETENTION_DAYS} days in ${BACKUP_DIR}"
find "${BACKUP_DIR}" -name 'yonixalpha-*.sql.gz' -mtime "+${RETENTION_DAYS}" -print -delete
