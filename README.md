# OpenDDIL Sensor Ingestion Gateway

This repository contains the **Sensor Ingestion Gateway** and the **Legacy Bridge** for the OpenDDIL architecture (Phase 7.1). It handles the ingestion of tactical sensor data via DDS, standardizes it using CloudEvents, and ensures a seamless transition for legacy systems using the Strangler Fig Pattern.

## Architecture Overview

The ingestion pipeline consists of two main components:

1. **RTI DDS Ingestor (`dds_ingestor.py`)**: A highly resilient Python agent that subscribes to RTI Connext DDS domains, parses `DynamicData` samples, and wraps them in a standard CloudEvents envelope. It then publishes these events to a local Redpanda/Kafka cluster on the `raw-sensor-stream` topic.
2. **The Strangler Fig Bridge (`redpanda-connect.yaml`)**: A Redpanda Connect (Benthos) configuration that consumes from the `tactical-events` topic and fans out the stream to two destinations:
   - **HQ Redpanda**: For DDIL-resilient global synchronization.
   - **Legacy RabbitMQ**: To feed legacy UI systems during the migration phase.

## Critical Data Formatting Rule

All telemetry pushed to the `raw-sensor-stream` topic **MUST** be wrapped in a standard CloudEvents JSON envelope. 

- **Kafka Message Key**: `device_id`
- **Payload Structure**:
  ```json
  {
    "id": "<UUID>",
    "source": "<device_id>",
    "type": "openddil.sensor.temperature",
    "time": "2026-04-08T12:00:00Z",
    "datacontenttype": "application/json",
    "data": { ... original sensor telemetry ... }
  }
  ```

## Getting Started

### Prerequisites
- Python 3.9+
- `confluent-kafka` Python package
- `rti.connextdds` Python package
- Redpanda / Kafka cluster running locally
- Redpanda Connect (Benthos) installed

### Running the DDS Ingestor
```bash
pip install confluent-kafka rti-connextdds
python dds_ingestor.py
```

### Running the Legacy Bridge
```bash
redpanda-connect run redpanda-connect.yaml
```
