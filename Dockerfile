# =============================================================================
# openddil-sensor-ingest — Binary DIS Ingestor (Phase 2)
# =============================================================================
# Two-stage build:
#   1. builder  — installs deps into a venv via uv + pyproject.toml
#   2. runtime  — slim image that runs dis_ingestor.py
# =============================================================================

# ---------- Stage 1: Builder ----------
FROM python:3.11-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc g++ make librdkafka-dev \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv && uv venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml .
RUN uv pip compile pyproject.toml -o requirements.txt \
    && uv pip install --no-cache -r requirements.txt

# ---------- Stage 2: Runtime ----------
FROM python:3.11-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        librdkafka1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY dis_ingestor.py /app/dis_ingestor.py
COPY fixtures /app/fixtures

EXPOSE 62040/udp
EXPOSE 8080/tcp

CMD ["python", "/app/dis_ingestor.py"]
