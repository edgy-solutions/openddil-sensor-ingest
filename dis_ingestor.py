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
from http.server import ThreadingHTTPServer
import logging
import os
import signal
import socket
import sys
import threading
import time

from io import BytesIO

from appearance import decode as decode_appearance

from opendis.PduFactory import createPdu
from opendis.dis7 import EntityStatePdu

from confluent_kafka import Producer, KafkaException
from prometheus_client import Counter, Histogram
from prometheus_client.exposition import MetricsHandler

import stall

# ---------------------------------------------------------------------------
# Configuration (environment variables with safe defaults)
# ---------------------------------------------------------------------------
# UDP_HOST is the UNICAST listen address: the address this sidecar binds.
# The default 0.0.0.0 accepts on every interface, which is right for a
# single sidecar on a host it owns. Set it to one address when a host runs
# more than one feed and each must land in a different sidecar -- that is
# the unicast form of the separation a site filter does for multicast.
UDP_HOST       = os.getenv("UDP_HOST",       "0.0.0.0")
UDP_PORT       = int(os.getenv("UDP_PORT",   "62040"))

# A DIS exercise is conventionally distributed on a multicast group, and a
# group is not a destination the kernel delivers by default: a plain bind()
# receives NOTHING from one until the socket has joined it. Unset means
# unicast, which is what every existing deployment is.
DIS_MULTICAST_GROUP = os.getenv("DIS_MULTICAST_GROUP", "").strip()
DIS_MULTICAST_IFACE = os.getenv("DIS_MULTICAST_IFACE", "0.0.0.0").strip()

# The DIS site this sidecar is responsible for, or None for "everything that
# arrives".
#
# WHY THIS EXISTS. Separation used to be a property of the WIRE: each
# sidecar had its own UDP port and the sender addressed the right one, so
# sensor-ingest could stay a pure transport and never look at an entity id.
# That works because a unicast port has exactly one reader. A multicast
# group has every reader -- every sidecar joined to the exercise receives
# every site's PDUs, and no amount of care at the sender changes that. So
# on a multicast feed the separation cannot live on the wire, and the only
# place left that knows which PDUs are whose is here.
#
# Unset is "accept everything", so existing port-separated deployments keep
# their exact behaviour and this stays opt-in.
_site = os.getenv("DIS_SITE_ID", "").strip()
DIS_SITE_ID: int | None = int(_site) if _site else None
UDP_BUFSIZE    = int(os.getenv("UDP_BUFSIZE", str(4 * 1024 * 1024)))  # 4 MB

KAFKA_BROKERS  = os.getenv("KAFKA_BROKERS",  "redpanda-edge:9092")
KAFKA_TOPIC    = os.getenv("KAFKA_TOPIC",    "ingress-dis-raw")
LOG_LEVEL      = os.getenv("LOG_LEVEL",      "INFO").upper()

# Origin-node provenance (ADR-0022 / ADR-0023). Stamped here, the earliest
# point in the pipeline that knows where the DIS feed physically lands.
# Values are deployer-assigned per the topology contract; this just stamps
# what the deployer told it to.
#
# Sensor-ingest was a pure UDP→Kafka transport, with entity separation left
# entirely to test-side discipline: the harness sent each range to its own
# UDP port and one port had one reader. That holds for unicast and only for
# unicast. A DIS exercise distributed on a multicast group delivers every
# site to every joined sidecar, so there is no addressing decision left at
# the sender to make the separation with. DIS_SITE_ID is the smallest thing
# that restores it -- one equality test on the site field the PDU already
# carries, no ranges, no ontology lookup, and off unless configured.
OPENDDIL_EDGE_ID   = os.getenv("OPENDDIL_EDGE_ID",   "edge-01")
OPENDDIL_REGION_ID = os.getenv("OPENDDIL_REGION_ID", "region-01")

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
DIS_PDUS_FILTERED = Counter(
    "dis_pdus_filtered_total",
    "DIS PDUs decoded but not this sidecar's to publish",
    ["reason"],
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
        return
    # CONFIRMED DELIVERY, not a successful produce() call. produce() enqueues
    # locally and returns fine while the producer is disconnected, so marking
    # progress there would report health exactly when there is none.
    stall.note_output()


# ---------------------------------------------------------------------------
# PDU → JSON extraction
# ---------------------------------------------------------------------------

def _appearance_raw(pdu) -> int:  # noqa: ANN001
    return int(getattr(pdu, "entityAppearance", 0))


def _appearance_decoded(pdu) -> dict:  # noqa: ANN001
    """Named facts from the appearance field, or {} if none may be read.

    The RAW bits are kept alongside deliberately: Stage 1 must not become the
    only place the truth lives, and a future ontology entry should be able to
    reinterpret a capture without re-ingesting it.
    """
    etype = pdu.entityType
    return decode_appearance(
        _appearance_raw(pdu),
        kind=int(getattr(etype, "entityKind", 0)),
        domain=int(getattr(etype, "domain", 0)),
        site_id=int(pdu.entityID.siteID),
    )


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
        "appearance_bits":         _appearance_raw(pdu),
        # Decoded facts, ontology-driven and domain-aware. ABSENT when the
        # source is not declared to populate appearance, when the field is
        # all-zero (silence, not "undamaged"), or when the kind/domain has no
        # entry. A consumer must read a missing key as NO CLAIM — never as a
        # negative. See appearance.py for both refusals.
        "appearance":              _appearance_decoded(pdu),
        "dead_reckoning_algorithm": int(getattr(pdu.deadReckoningParameters, "deadReckoningAlgorithm", 0)),
        "pdu_sequence":             0,  # PDU sequence not in opendis EntityStatePdu header
        "ingest_timestamp":         datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "_raw_size_bytes":          raw_size,
        # Origin-node provenance — see module header. DIS-mapper Bloblang
        # reads this and writes it to Silver Provenance.edge_id/region_id.
        "origin_node": {
            "edge_id":   OPENDDIL_EDGE_ID,
            "region_id": OPENDDIL_REGION_ID,
        },
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

def _serve_http() -> None:
    """/metrics plus /healthz/live, on the port the chart already scrapes.

    Subclasses the prometheus handler rather than running a second server:
    one port, one thing to configure, and the liveness answer comes from the
    same process whose progress it describes.
    """
    class _Handler(MetricsHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/healthz/live"):
                st = stall.state()
                body = json.dumps(st).encode()
                # 503 is what makes the kubelet restart this pod. A wedge that
                # answers 200 is the failure this endpoint exists to end.
                self.send_response(503 if st["stalled"] else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def log_message(self, *_a):  # noqa: ANN002
            pass  # kubelet probes every few seconds; do not narrate them

    srv = ThreadingHTTPServer(("0.0.0.0", PROMETHEUS_PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True,
                     name="metrics-health").start()


def run():
    global _producer  # noqa: PLW0603

    # Metrics AND the liveness endpoint on one server. The stall state lives
    # in-process because the component already knows its own progress; see
    # stall.py for why this gates LIVENESS rather than readiness.
    _serve_http()
    logger.info("Prometheus /metrics and /healthz/live listening on :%d",
                PROMETHEUS_PORT)

    # Connect to Kafka (blocks until ready or shutdown)
    _producer = _build_producer()

    # Start stats logger thread
    stats_thread = threading.Thread(target=_stats_logger, daemon=True)
    stats_thread.start()

    # Open UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UDP_BUFSIZE)

    if DIS_MULTICAST_GROUP:
        # SO_REUSEADDR before bind, and it is not boilerplate here: several
        # sidecars are MEANT to sit on one group and one port, each taking
        # its own site. Without it the second one to start fails to bind and
        # the deployment silently has one fewer ingester than it was told to
        # have.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Bind the wildcard, not the group: binding the group address is
        # accepted on Linux but not portable, and the wildcard is what lets
        # the same container also answer a unicast probe on this port.
        sock.bind(("0.0.0.0", UDP_PORT))
        mreq = (socket.inet_aton(DIS_MULTICAST_GROUP)
                + socket.inet_aton(DIS_MULTICAST_IFACE))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        logger.info(
            "Joined multicast group %s on iface %s, listening UDP :%d "
            "(SO_RCVBUF=%d MB)",
            DIS_MULTICAST_GROUP, DIS_MULTICAST_IFACE, UDP_PORT,
            UDP_BUFSIZE // (1024 * 1024),
        )
    else:
        sock.bind((UDP_HOST, UDP_PORT))
        logger.info(
            "Listening on UDP %s:%d (unicast, SO_RCVBUF=%d MB)",
            UDP_HOST, UDP_PORT, UDP_BUFSIZE // (1024 * 1024),
        )

    sock.settimeout(1.0)  # Non-blocking so we can honour _shutdown

    # Say the scope out loud at startup. An operator reading one line of log
    # should be able to tell a sidecar that is ignoring most of the feed on
    # purpose from one that is not receiving it.
    if DIS_SITE_ID is None:
        logger.info("Site filter: OFF -- publishing every site that arrives")
    else:
        logger.info("Site filter: site %d ONLY -- all other sites counted "
                    "in dis_pdus_filtered_total and dropped", DIS_SITE_ID)

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

            # --- Filter: only this sidecar's site ---
            #
            # THE POSITION OF THIS BLOCK IS LOAD-BEARING. It sits before
            # stall.note_input(), and moving it after would ship a restart
            # loop.
            #
            # The stall condition is `input advanced AND output did not`
            # (see stall.py). On a shared multicast group this sidecar
            # receives every site's PDUs, so if another site is busy while
            # ours is quiet -- a vehicle parked, a sub-exercise not yet
            # started, an entirely ordinary state -- counting those PDUs as
            # OUR input would make the condition true continuously: input
            # advancing, output correctly zero. Liveness answers 503, the
            # kubelet restarts a pod that is doing its job perfectly, and it
            # does so every window for as long as the other site keeps
            # talking. That is the shape stall.py's own module note warns
            # about for relays: failing closed on an absence that was always
            # expected, arriving dressed as a liveness probe.
            #
            # So a PDU for another site is not this sidecar's input. It is
            # counted, because silently vanishing traffic is how a
            # misconfigured DIS_SITE_ID hides, and dropped.
            if DIS_SITE_ID is not None and int(pdu.entityID.siteID) != DIS_SITE_ID:
                DIS_PDUS_FILTERED.labels(reason="site").inc()
                continue

            stall.note_input()

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
                # A FATAL PRODUCER ERROR IS NOT A WARNING. librdkafka marks an
                # error fatal when the producer instance can never publish
                # again -- the client is dead and only a new one will do. This
                # handler logged it at WARNING and continued the loop, so the
                # process stayed up, kept receiving UDP, kept decoding, and
                # published nothing. Measured on edge-01 2026-09-09: 460,181
                # decoded, 21,264 kafka errors, pod 1/1 Running with zero
                # restarts, three and a half hours.
                #
                # A PROCESS THAT CANNOT DO ITS JOB MUST SAY SO BY DYING, NOT BY
                # COUNTING. Exiting non-zero lets Kubernetes restart it, and a
                # crash loop is visible where a wedge is not -- that is the
                # entire value of the fatal classification librdkafka already
                # gives us and this code was discarding.
                err = exc.args[0] if exc.args else None
                if err is not None and getattr(err, "fatal", None) and err.fatal():
                    logger.critical(
                        "FATAL Kafka producer state (%s) — the producer cannot "
                        "publish again. Exiting so the pod restarts; staying up "
                        "would keep decoding into a dead client.", err)
                    # Flush is pointless on a fatal producer and would block
                    # the exit for its full timeout.
                    os._exit(70)   # EX_SOFTWARE
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
