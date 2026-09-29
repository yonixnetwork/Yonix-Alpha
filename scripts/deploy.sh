#!/bin/bash
# Redeploys the current branch's latest commit. Run from the repo root on
# the server (e.g. /opt/yonixalpha) after scripts/bootstrap-server.sh has
# already set the box up once — see docs/DEPLOYMENT.md.
#
# Migrations run automatically: apps/api's own Dockerfile CMD is
# `alembic upgrade head && uvicorn ...`, so a fresh `api` container always
# brings the schema up to date before serving traffic — this script
# doesn't need (and shouldn't have) a separate migration step.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"
# --env-file .env: Compose's default top-level .env lookup is relative to
# the *first -f file's directory* (infra/docker/), not the repo root where
# .env actually lives - without this flag, ${VAR} interpolation in
# build.args (e.g. web's NEXT_PUBLIC_API_URL) silently resolves to "" even
# though env_file: (a literal path, unaffected) supplies the right value to
# the running container. Verified against a real `docker compose config`
# run - see docs/DEPLOYMENT.md section 2.2.
COMPOSE="docker compose --env-file .env -f infra/docker/docker-compose.yml -f infra/docker/docker-compose.prod.yml"

if [ ! -f .env ]; then
    echo "No .env in ${REPO_ROOT} — copy .env.example and fill in real values before deploying (see docs/DEPLOYMENT.md)." >&2
    exit 1
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
echo "==> Pulling latest ${BRANCH}"
git fetch origin "${BRANCH}"
git merge --ff-only "origin/${BRANCH}"

echo "==> Building images"
${COMPOSE} build

echo "==> Starting/updating the stack"
# --remove-orphans stops containers of services no longer in the compose
# files (e.g. the removed data-binance / engine-binance-futures /
# execution-futures), so no old image keeps running against the database.
${COMPOSE} up -d --remove-orphans

echo "==> Waiting for api to report healthy"
ATTEMPTS=0
until ${COMPOSE} ps api --format json | grep -q '"Health":"healthy"'; do
    ATTEMPTS=$((ATTEMPTS + 1))
    if [ "${ATTEMPTS}" -ge 30 ]; then
        echo "api did not become healthy within 60s — check 'docker compose logs api'." >&2
        exit 1
    fi
    sleep 2
done
echo "    api is healthy."

echo "==> Pruning dangling images from previous builds"
docker image prune -f >/dev/null

echo "==> Deploy complete: $(git rev-parse --short HEAD)"
