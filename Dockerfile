FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# psycopg2-binary, numpy, and pandas all ship manylinux wheels, so no compiler
# or system libpq is required — pip installs prebuilt binaries.
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY collector.py .

RUN useradd --create-home --uid 1000 collector
USER collector

# DB connection is supplied at runtime via env vars:
#   DB_HOST, DB_NAME, DB_USER, DB_PASSWORD
# CLI args pass through to the collector, e.g.:
#   docker run --rm dam-release-collector --lookback 1h
ENTRYPOINT ["python", "collector.py"]
