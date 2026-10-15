# Build from the repo root: docker build -f apps/api/Dockerfile .
# (this service depends on packages/core-py, which sits outside its own
# directory, so the build context must be the repo root — see docker-compose.yml).
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY packages/core-py /packages/core-py
COPY packages/crypto-py /packages/crypto-py
COPY apps/api/requirements.txt .
RUN sed -i -e 's#../../packages/core-py#/packages/core-py#' -e 's#../../packages/crypto-py#/packages/crypto-py#' requirements.txt \
    && pip install -r requirements.txt

COPY apps/api .

RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# The long start period covers `alembic upgrade head` on a large database:
# failures during it don't count, and the first passing check still marks
# the container healthy at once. Workers wait for this (docker-compose.yml).
HEALTHCHECK --interval=15s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

# The crypto schema (Crypto Trading engine) has its own migration history in
# its own schema. A failure there never stops the API: the Crypto Trading
# page then reports "schema not installed" and everything else runs as before.
CMD ["sh", "-c", "alembic upgrade head && { python -m yonixalpha_crypto.migrate upgrade || echo 'crypto schema migration failed; the Crypto Trading engine stays unavailable'; } && uvicorn app.main:app --host ${APP_HOST:-0.0.0.0} --port ${APP_PORT:-8000}"]
