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
COPY apps/api/requirements.txt .
RUN sed -i 's#../../packages/core-py#/packages/core-py#' requirements.txt \
    && pip install -r requirements.txt

COPY apps/api .

RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host ${APP_HOST:-0.0.0.0} --port ${APP_PORT:-8000}"]
