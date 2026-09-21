# Build from the repo root: docker build -f services/data-solana/Dockerfile .
# (this service depends on packages/core-py, which sits outside its own
# directory, so the build context must be the repo root — see docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY packages/core-py /packages/core-py
COPY services/data-solana/requirements.txt .
RUN sed -i 's#../../packages/core-py#/packages/core-py#' requirements.txt \
    && pip install -r requirements.txt

COPY services/data-solana/app ./app

RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "app.main"]
