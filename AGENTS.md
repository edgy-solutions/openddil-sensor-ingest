# OpenDDIL Agents Documentation

## Role and Context
You are an AI agent working on the `openddil-sensor-ingest` repository. This repository is part of the OpenDDIL architecture (Phase 7.1) and is responsible for tactical sensor data ingestion.

## Core Directives

1. **CloudEvents Standard**: Any modifications to data ingestion or publishing MUST strictly adhere to the CloudEvents Protobuf envelope format from the centralized contracts.
   - `id`: UUID string.
   - `source`: The `device_id`.
   - `type`: E.g., `openddil.sensor.temperature` or dynamic based on `dds_type`.
   - `time`: ISO-8601 UTC timestamp.
   - `datacontenttype`: `application/protobuf`.
   - `data`: The packed protobuf telemetry payload.
   - The Kafka message key MUST ALWAYS be the `device_id`.

2. **Resilience (DDIL)**: 
   - OpenDDIL operates in Denied, Disrupted, Intermittent, and Limited environments.
   - Kafka producers must use `acks=all` and infinite retries.

3. **Dynamic Configuration**:
   - All topics, broker URLs, and sensor types must be dynamically loaded from `config.yaml` and validated using the Pydantic models in `config.py`.
   - Do not hardcode topics or sensor mappings in the Python scripts.

5. **Dependencies**:
   - Prefer `confluent_kafka` over `kafka-python` for performance and reliability.
   - DDS interactions must use the official `rti.connextdds` Python API.
   - Configuration requires `pydantic` and `pyyaml`.

6. **Docker Compose discipline (cross-repo rule)**:
   - When this image is consumed by `openddil-demo/docker-compose.yml`,
     the base compose references `image: ghcr.io/edgy-solutions/openddil/sensor-ingest:latest`.
   - The base compose MUST NOT contain a `build:` directive for this
     service. Builds + source mounts live in
     `openddil-demo/docker-compose.override.yml`.
   - **When you change this repo's Dockerfile or pyproject.toml, publish
     a new image to `ghcr.io/edgy-solutions/openddil/sensor-ingest:latest`** (or bump a
     tag and update the demo's base compose to match) so a customer or
     CI runner with only the base compose still gets the updated sidecar.

7. **Customer-encumbered code does NOT live here**:
   - Anything that hardcodes customer-specific JSON field names,
     RabbitMQ exchange names, or other customer ICD details lives in
     the sibling `openddil-customer-bundle/sensor-ingest/` directory.
     This OSS image bakes only the DIS binary sidecar (`dis_ingestor.py`)
     and shared infra. Customer sidecars (proprietary HTTP, etc.) are
     bind-mounted into containers that reuse this OSS image at runtime.