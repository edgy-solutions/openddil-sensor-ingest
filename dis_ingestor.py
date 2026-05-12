"""
=============================================================================
OpenDDIL Sensor Ingest — Binary DIS PDU Sidecar
=============================================================================
Listens on UDP 0.0.0.0:62040, parses binary IEEE 1278.1 DIS Entity State PDUs
via opendis==1.0, and publishes structured JSON to Kafka topic ingress-dis-raw.

Architectural placement:
  [ DIS Simulator (VR-Forces / AFSIM / SIMDIS) ]
           │ binary UDP, port 62040
           ▼
  [ dis_ingestor.py  ← THIS FILE ]
           │ JSON, key = entity_id_urn
           ▼
  [ Kafka: ingress-dis-raw ]  (Bronze, per ADR-0010)
           │
           ▼
  [ Redpanda Connect + sim-dis-mapping.yaml ]  (Bloblang + ontology)

Key design rules:
  - DO NOT inject mock thermal/fuel/power data.  DIS does not carry
    sustainment metrics; the Protobuf schema makes them optional.
  - No arithmetic in this file — unit conversion belongs in algorithms.py.
  - Kafka unavailable on startup → exponential backoff up to 60 s.
  - SIGTERM → flush(10 s) then clean exit.
  - Prometheus /metrics on :8080.

Library verification (2026-05-12, Task 1):
  opendis==1.0, confluent-kafka==2.14.0, prometheus-client==0.25.0
=============================================================================
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import signal
import socket
import sys
import threading
import time

from io import BytesIO

from opendis.PduFactory import createPdu
from opendis.dis7 import EntityStatePdu

from confluent_kafka import Producer, KafkaException
from prometheus_client import Counter, Histogram, start_http_server

# ---------------------------------------------------------------------------
# Configuration (environment variables with safe defaults)
# ---------------------------------------------------------------------------
UDP_HOST       = os.getenv("UDP_HOST",       "0.0.0.0")
UDP_PORT       = int(os.getenv("UDP_PORT",   "62040"))
UDP_BUFSIZE    = int(os.getenv("UDP_BUFSIZE", str(4 * 1024 * 1024)))  # 4 MB

KAFKA_BROKERS  = os.getenv("KAFKA_BROKERS",  "redpanda-edge:9092")
KAFKA_TOPIC    = os.getenv("KAFKA_TOPIC",    "ingress-dis-raw")
LOG_LEVEL      = os.getenv("LOG_LEVEL",      "INFO").upper()

PROMETHEUS_PORT = int(os.getenv("PROMETHEUS_PORT", "8080"))

KAFKA_BACKOFF_MAX_S = 60  # Maximum backoff before giving up on producer connect

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("dis_ingestor")

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
DIS_PDUS_RECEIVED = Counter(
    "dis_pdus_received_total",
    "Total DIS PDUs received on the UDP socket",
    ["pdu_type"],
)
DIS_PDUS_DECODED = Counter(
    "dis_pdus_decoded_total",
    "Total DIS PDUs successfully decoded",
)
DIS_DECODE_ERRORS = Counter(
    "dis_decode_errors_total",
    "Total DIS PDU decode failures",
)
KAFKA_PUBLISH_ERRORS = Counter(
    "kafka_publish_errors_total",
    "Total Kafka publish errors",
)
KAFKA_PUBLISH_LATENCY = Histogram(
    "kafka_publish_latency_seconds",
    "Kafka publish round-trip latency in seconds",
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
_producer: Producer | None = None
_shutdown = threading.Event()

# ---------------------------------------------------------------------------
# SIGTERM handler
# ---------------------------------------------------------------------------
def _handle_sigterm(signum, frame):  # noqa: ANN001
    logger.info("SIGTERM received — flushing Kafka producer and shutting down...")
    _shutdown.set()


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT, _handle_sigterm)

# ---------------------------------------------------------------------------
# Kafka producer with exponential backoff
# ---------------------------------------------------------------------------
def _build_producer() -> Producer:
    conf = {
        "bootstrap.servers": KAFKA_BROKERS,
        "acks":               "all",
        "linger.ms":          20,
        "compression.type":   "zstd",
        "enable.idempotence": True,
    }
    backoff = 1.0
    while not _shutdown.is_set():
        try:
            producer = Producer(conf)
            # Verify connectivity — list topics (raises on broker unavailable)
            producer.list_topics(timeout=5)
            logger.info("Kafka producer ready (brokers=%s, topic=%s)", KAFKA_BROKERS, KAFKA_TOPIC)
            return producer
        except KafkaException as exc:
            logger.warning(
                "Kafka not available yet (%s). Retrying in %.0f s...", exc, backoff
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, KAFKA_BACKOFF_MAX_S)
    logger.info("Shutdown requested while waiting for Kafka.")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Delivery callback
# ---------------------------------------------------------------------------
def _on_delivery(err, msg):  # noqa: ANN001
    if err:
        KAFKA_PUBLISH_ERRORS.inc()
        logger.warning("Kafka delivery error: %s", err)


# ---------------------------------------------------------------------------
# PDU → JSON extraction
# ---------------------------------------------------------------------------
def _extract_entity_state(pdu: EntityStatePdu, raw_size: int) -> dict:  # noqa: ANN001
    """
    Extract fields from a decoded EntityStatePdu into the JSON structure
    expected by sim-dis-mapping.yaml.

    IMPORTANT: DO NOT add sustainment (thermal/fuel/power) fields here.
    DIS Entity State PDUs do not carry sustainment data. The Protobuf
    schema makes sustainment fields optional. Absence is correct behaviour.
    """
    eid   = pdu.entityID
    etype = pdu.entityType
    loc   = pdu.entityLocation
    vel   = pdu.entityLinearVelocity
    orient = pdu.entityOrientation
    marking_raw = getattr(pdu, "marking", None)

    if marking_raw is not None:
        try:
            marking = bytes(marking_raw.characters).rstrip(b"\x00").decode("ascii", errors="replace")
        except Exception:
            marking = ""
    else:
        marking = ""

    entity_id_urn = f"dis:{eid.siteID}:{eid.applicationID}:{eid.entityID}"

    return {
        "dis_entity_id": {
            "site":        eid.siteID,
            "application": eid.applicationID,
            "entity":      eid.entityID,
        },
        "entity_id_urn": entity_id_urn,
        "dis_entity_type": {
            "kind":        etype.entityKind,
            "domain":      etype.domain,
            "country":     etype.country,
            "category":    etype.category,
            "subcategory": etype.subcategory,
            "specific":    etype.specific,
            "extra":       etype.extra,
        },
        "marking":      marking,
        "force_id":     int(pdu.forceId),
        "location_ecef": {
            "x": loc.x,
            "y": loc.y,
            "z": loc.z,
        },
        "linear_velocity_ecef": {
            "x": vel.x,
            "y": vel.y,
            "z": vel.z,
        },
        "orientation_euler": {
            "psi":   orient.psi,
            "theta": orient.theta,
            "phi":   orient.phi,
        },
        "appearance_bits":         int(getattr(pdu, "entityAppearance", 0)),
        "dead_reckoning_algorithm": int(getattr(pdu.deadReckoningParameters, "deadReckoningAlgorithm", 0)),
        "pdu_sequence":             0,  # PDU sequence not in opendis EntityStatePdu header
        "ingest_timestamp":         datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "_raw_size_bytes":          raw_size,
    }


# ---------------------------------------------------------------------------
# Periodic stats logger
# ---------------------------------------------------------------------------
def _stats_logger(interval_s: int = 60):
    """Log a counter snapshot every `interval_s` seconds."""
    while not _shutdown.wait(timeout=interval_s):
        logger.info(
            "Stats snapshot — received=%s decoded=%s decode_errors=%s kafka_errors=%s",
            DIS_PDUS_RECEIVED.labels(pdu_type="1")._value.get()
            if hasattr(DIS_PDUS_RECEIVED.labels(pdu_type="1"), "_value") else "n/a",
            DIS_PDUS_DECODED._value.get()
            if hasattr(DIS_PDUS_DECODED, "_value") else "n/a",
            DIS_DECODE_ERRORS._value.get()
            if hasattr(DIS_DECODE_ERRORS, "_value") else "n/a",
            KAFKA_PUBLISH_ERRORS._value.get()
            if hasattr(KAFKA_PUBLISH_ERRORS, "_value") else "n/a",
        )


# ---------------------------------------------------------------------------
# Main receive loop
# ---------------------------------------------------------------------------
def run():
    global _producer  # noqa: PLW0603

    # Start Prometheus metrics server
    start_http_server(PROMETHEUS_PORT)
    logger.info("Prometheus /metrics listening on :%d", PROMETHEUS_PORT)

    # Connect to Kafka (blocks until ready or shutdown)
    _producer = _build_producer()

    # Start stats logger thread
    stats_thread = threading.Thread(target=_stats_logger, daemon=True)
    stats_thread.start()

    # Open UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UDP_BUFSIZE)
    sock.bind((UDP_HOST, UDP_PORT))
    sock.settimeout(1.0)  # Non-blocking so we can honour _shutdown
    logger.info(
        "Listening on UDP %s:%d (SO_RCVBUF=%d MB)",
        UDP_HOST, UDP_PORT, UDP_BUFSIZE // (1024 * 1024)
    )

    try:
        while not _shutdown.is_set():
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                _producer.poll(0)  # Service delivery callbacks
                continue

            # --- Decode PDU ---
            pdu = None
            try:
                pdu = createPdu(data)
            except Exception as exc:
                DIS_DECODE_ERRORS.inc()
                logger.debug("PDU decode error from %s: %s", addr, exc)
                continue

            if pdu is None:
                DIS_DECODE_ERRORS.inc()
                logger.debug("createPdu returned None for %d bytes from %s", len(data), addr)
                continue

            pdu_type = int(getattr(pdu, "pduType", -1))
            DIS_PDUS_RECEIVED.labels(pdu_type=str(pdu_type)).inc()

            # --- Filter: only Entity State PDUs (type 1) ---
            if pdu_type != 1:
                logger.debug("Dropped PDU type %d from %s (not Entity State)", pdu_type, addr)
                continue

            if not isinstance(pdu, EntityStatePdu):
                DIS_DECODE_ERRORS.inc()
                logger.debug("PDU type=1 but not EntityStatePdu from %s — dropping", addr)
                continue

            DIS_PDUS_DECODED.inc()

            # --- Extract to JSON ---
            try:
                payload = _extract_entity_state(pdu, len(data))
            except Exception as exc:
                DIS_DECODE_ERRORS.inc()
                logger.warning("Field extraction error from %s: %s", addr, exc)
                continue

            key = payload["entity_id_urn"]
            body = json.dumps(payload, separators=(",", ":")).encode()

            # --- Publish to Kafka ---
            t0 = time.monotonic()
            try:
                _producer.produce(
                    topic=KAFKA_TOPIC,
                    key=key,
                    value=body,
                    on_delivery=_on_delivery,
                )
                _producer.poll(0)  # Non-blocking delivery callback service
                KAFKA_PUBLISH_LATENCY.observe(time.monotonic() - t0)
            except BufferError:
                # Producer queue full — log and drop rather than block
                KAFKA_PUBLISH_ERRORS.inc()
                logger.warning("Kafka producer queue full — dropping PDU %s", key)
            except KafkaException as exc:
                KAFKA_PUBLISH_ERRORS.inc()
                logger.warning("Kafka produce error for %s: %s", key, exc)

    finally:
        sock.close()
        if _producer:
            logger.info("Flushing Kafka producer (timeout=10 s)...")
            remaining = _producer.flush(timeout=10)
            if remaining > 0:
                logger.warning("%d message(s) not flushed before shutdown", remaining)
        logger.info("dis_ingestor shutdown complete.")


if __name__ == "__main__":
    run()
