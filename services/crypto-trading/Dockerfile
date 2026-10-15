# Build from the repo root: docker build -f services/crypto-trading/Dockerfile .
# Copies only packages/crypto-py: this image does not contain, import or
# depend on packages/core-py, so it never changes when Memecoin code changes
# and Memecoin images never change because of it.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY packages/crypto-py /packages/crypto-py
COPY services/crypto-trading/requirements.txt .
RUN sed -i 's#../../packages/crypto-py#/packages/crypto-py#' requirements.txt \
    && pip install -r requirements.txt

COPY services/crypto-trading/app ./app

RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "app.main"]
