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