# ---- Builder: compile dependencies (psycopg2 needs gcc + libpq headers) ----
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Build all dependencies into an isolated virtualenv we can copy wholesale.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt

# ---- Runtime: slim image with only the libpq runtime library ----
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 1000 collector

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY collector.py .

USER collector

# DB connection is supplied at runtime via env vars:
#   DB_HOST, DB_NAME, DB_USER, DB_PASSWORD
# CLI args pass through to the collector, e.g.:
#   docker run --rm dam-release-collector --lookback 1h
ENTRYPOINT ["python", "collector.py"]
