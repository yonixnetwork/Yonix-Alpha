# Build from the repo root: docker build -f services/engine-solana-migration/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY packages/core-py /packages/core-py
COPY services/engine-solana-migration/requirements.txt .
RUN sed -i 's#../../packages/core-py#/packages/core-py#' requirements.txt \
    && pip install -r requirements.txt

COPY services/engine-solana-migration/app ./app

RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "app.main"]
