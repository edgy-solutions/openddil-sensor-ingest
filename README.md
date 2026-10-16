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

## Nation stamping
`DIS_NATION_RULES` is a JSON object with `overrides` (entity id -> nation) and `rules` (first match wins, kind-1 entities only). A matched entity's records carry `stamped_nation` in their origin block; with no nation the key is omitted. An override value of the literal string `"exempt"` keeps that entity out of every rule: it is never stamped. It is a string rather than JSON null because Helm drops null-valued keys when it merges values files.

## Readiness

`/healthz/ready` on the metrics port answers 200 always, with the real state in the JSON body (`ready`, `gates_pod`, the entity counters) and in the `dis_sidecar_ready` gauge (0/1), which is the signal to alert on. By default it gates nothing, so a sidecar can never pull a co-located simulator out of its Services. Set `READY_GATES_POD=1` to restore the gate (503 when not ready) for a pod with no simulator behind a Service.
