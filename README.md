# OpenDDIL Sensor Ingestion Gateway

This repository contains the entry points for all physical sensors entering the OpenDDIL architecture.

## The CloudEvents Mandate
**Rule:** No raw telemetry is allowed on the `raw-sensor-stream` Redpanda topic. 

All ingestors (DDS, RabbitMQ, HTTP) MUST wrap their payloads in a CloudEvents Protobuf envelope defined in the centralized contracts. The Kafka Message Key MUST be the device ID.

**Example Protobuf Structure:**
```protobuf
message CloudEvent {
  string id = 1;
  string source = 2;
  string type = 3;
  string time = 4;
  string datacontenttype = 5; // "application/protobuf"
  google.protobuf.Any data = 6;
}
```

## Configuration Engine
The ingestion gateway is fully data-driven. Topics, broker URLs, and sensor mappings are defined dynamically in `config.yaml` and validated via Pydantic in `config.py`. Do not hardcode these values in the Python scripts.

## Running the Ingestors

* **RTI DDS Ingestor:** Run `python dds_ingestor.py --domain-id 0`. This listens to the DDS mesh and wraps the payloads for Redpanda using the Schema Registry.
