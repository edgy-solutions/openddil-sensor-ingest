# OpenDDIL Agents Documentation

## Role and Context
You are an AI agent working on the `openddil-sensor-ingest` repository. This repository is part of the OpenDDIL architecture (Phase 7.1) and is responsible for tactical sensor data ingestion and legacy system bridging.

## Core Directives

1. **CloudEvents Standard**: Any modifications to data ingestion or publishing MUST strictly adhere to the CloudEvents JSON envelope format.
   - `id`: UUID string.
   - `source`: The `device_id`.
   - `type`: E.g., `openddil.sensor.temperature` or dynamic based on `dds_type`.
   - `time`: ISO-8601 UTC timestamp.
   - `datacontenttype`: `application/json`.
   - `data`: The raw telemetry dictionary.
   - The Kafka message key MUST ALWAYS be the `device_id`.

2. **Resilience (DDIL)**: 
   - OpenDDIL operates in Denied, Disrupted, Intermittent, and Limited environments.
   - Kafka producers must use `acks=all` and infinite retries.
   - Redpanda Connect pipelines must use `max_retries: 0` (which implies infinite retries in Benthos/Redpanda Connect for outputs) when communicating with HQ.

3. **Strangler Fig Pattern**:
   - We are actively migrating away from legacy RabbitMQ systems.
   - Never break the `legacy_sensor_exchange` AMQP output in `redpanda-connect.yaml` until the migration is officially marked as complete.
   - The fan-out pattern is critical to keep both the new OpenDDIL architecture and the legacy UI functioning simultaneously.

4. **Dynamic Configuration**:
   - All topics, broker URLs, and sensor types must be dynamically loaded from `config.yaml` and validated using the Pydantic models in `config.py`.
   - Do not hardcode topics or sensor mappings in the Python scripts.

5. **Dependencies**:
   - Prefer `confluent_kafka` over `kafka-python` for performance and reliability.
   - DDS interactions must use the official `rti.connextdds` Python API.
   - Configuration requires `pydantic` and `pyyaml`.