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
df -h / | tail -1 | awk '{print "    disk: " $4 " free of " $2}'
if [ "${DEPLOY_PARALLEL_BUILD:-0}" = "1" ]; then
    ${COMPOSE} build
else
    # One service at a time, each retried: on the 2 vCPU / 2 GB server, ten
    # parallel image exports ran past BuildKit's deadline ("failed to solve:
    # Internal: context deadline exceeded", 2026-10-01) and nothing was
    # deployed. Cached layers make the sequential build only a little slower.
    # DEPLOY_PARALLEL_BUILD=1 restores the parallel build on a bigger box.
    for svc in $(${COMPOSE} config --services); do
        for attempt in 1 2 3; do
            if ${COMPOSE} build "${svc}"; then
                break
            fi
            if [ "${attempt}" -eq 3 ]; then
                echo "Building ${svc} failed 3 times; the running stack was NOT changed." >&2
                echo "If the disk is nearly full, free build cache with: docker builder prune -f" >&2
                exit 1
            fi
            echo "    building ${svc} failed (attempt ${attempt}); retrying in 15s"
            sleep 15
        done
    done
fi

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

# Build cache grows by several GB per deploy (one layer set per service and
# commit) and once filled the disk. Cache not used in the last 48 hours is
# dropped; recent layers stay, so the next deploy still builds quickly.
# BUILD_CACHE_KEEP=168h keeps a week; a failed prune never fails the deploy.
echo "==> Pruning build cache unused for ${BUILD_CACHE_KEEP:-48h}"
docker builder prune -f --filter "until=${BUILD_CACHE_KEEP:-48h}" >/dev/null || echo "    build cache prune failed (ignored)"

echo "==> Deploy complete: $(git rev-parse --short HEAD)"
