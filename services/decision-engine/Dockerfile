# Build from the repo root: docker build -f services/decision-engine/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY packages/core-py /packages/core-py
COPY services/decision-engine/requirements.txt .
RUN sed -i 's#../../packages/core-py#/packages/core-py#' requirements.txt \
    && pip install -r requirements.txt

COPY services/decision-engine/app ./app

RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "app.main"]
