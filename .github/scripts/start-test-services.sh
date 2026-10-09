#!/usr/bin/env bash
# Starts the Postgres and Redis containers the Python tests use.
#
# GitHub's `services:` block pulls only from Docker Hub and fails the whole
# job before checkout when Docker Hub refuses the pull (seen 2026-10-09 on
# the merge of PR #66: every job stopped at "Initialize containers", twice).
# Here each image is pulled with retries, first from Docker Hub, then from
# the official-image mirror on Amazon ECR Public (same images, same tags).
set -euo pipefail

pull() {
  local name=$1
  local attempt registry
  for attempt in 1 2 3; do
    for registry in docker.io/library public.ecr.aws/docker/library; do
      if docker pull -q "$registry/$name" >/dev/null; then
        echo "$registry/$name"
        return 0
      fi
      echo "pull of $registry/$name failed (attempt $attempt)" >&2
    done
    sleep $((attempt * 10))
  done
  echo "could not pull $name from any registry" >&2
  return 1
}

PG=$(pull postgres:16-alpine)
REDIS=$(pull redis:7-alpine)

docker run -d --name ci-postgres -p 5432:5432 \
  -e POSTGRES_USER=yonixalpha \
  -e POSTGRES_PASSWORD=yonixalpha_test_pw \
  -e POSTGRES_DB=yonixalpha_test \
  "$PG" >/dev/null
docker run -d --name ci-redis -p 6379:6379 "$REDIS" >/dev/null

for _ in $(seq 1 60); do
  # -h 127.0.0.1: the image's first-start init server listens on the
  # socket only, so a TCP check is ready only once the real server is up.
  if docker exec ci-postgres pg_isready -h 127.0.0.1 -U yonixalpha -d yonixalpha_test >/dev/null 2>&1 \
    && docker exec ci-redis redis-cli ping >/dev/null 2>&1; then
    echo "postgres ($PG) and redis ($REDIS) ready"
    exit 0
  fi
  sleep 2
done
echo "postgres/redis did not become ready" >&2
docker logs ci-postgres >&2 || true
docker logs ci-redis >&2 || true
exit 1
