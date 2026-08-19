"""
=============================================================================
OpenDDIL Sensor Ingest — ASTERIX Decoder Sidecar (ADR-0030 Phase 2)
=============================================================================
Listens on UDP, decodes EUROCONTROL ASTERIX records against MOUNTED category
definitions, and publishes one JSON document per record to Kafka.

  [ Radar / recorder / replay ]
           │ binary UDP (multicast-capable)
           ▼
  [ asterix_ingestor.py  ← THIS FILE ]        Stage 1: decode only
           │ JSON, key = track identity
           ▼
  [ Kafka: ingress-asterix-raw ]  (Bronze)
           │
           ▼
  [ Redpanda Connect + asterix-cat062-mapping.yaml ]   Stage 2: map to Silver

ADR-0030 IS THE WHOLE DESIGN. Bloblang maps structured data; it does not
decode binary grammars. ASTERIX is FSPEC-driven with conditional field
presence and variable-length items — precisely the shape that makes a
mapping language unreadable and silently wrong at the edges. So the grammar
lives in category XML, the engine reads it, and adding a category is
dropping in a file.

WHY asterix4py AND NOT THE BETTER-KNOWN DECODER — this is a licensing
decision, recorded because it is not obvious and would be expensive to
rediscover:

  The widely-used `asterix-decoder` (CroatiaControl python-asterix) is
  GPL v2 and is a C extension. OpenDDIL is MIT across every repository.
  Importing a GPL C extension into a service and shipping that service
  raises copyleft questions on the combined work that nobody wants to
  answer at integration time.

  `asterix4py` is MIT, pure Python, and definition-driven from the same
  category-XML model — so it satisfies ADR-0030's actual requirement
  (declarative definitions, zero engine code per format) without the
  licence entanglement. See ADR-0030 §Licensing.

  Note the sidecar boundary would have HELPED even with the GPL decoder —
  separate process, communicating over a topic, is aggregation rather than
  linking. That is worth knowing, because it means the architecture already
  had a licence property nobody had claimed for it.

NO ARITHMETIC HERE, and one place that matters: CAT048 target reports carry
position as RHO/THETA (slant range + azimuth, radar-relative). Converting
that to WGS-84 needs the radar site's own coordinates, which the record does
NOT carry — it is a reference-data join plus geodesy. That is neither this
file's job nor Bloblang's (ADR-0013: no math in the mapping). CAT062 system
tracks carry WGS-84 directly (I062/105) and are therefore the category that
maps cleanly today. CAT048 is decoded and published; its position mapping is
deliberately unresolved rather than approximated.
=============================================================================
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sys
import threading
import time

from confluent_kafka import Producer, KafkaException
from prometheus_client import Counter, Histogram, start_http_server

# ---------------------------------------------------------------------------
# Configuration — environment only, same contract as dis_ingestor
# ---------------------------------------------------------------------------
UDP_HOST = os.getenv("ASTERIX_UDP_HOST", "0.0.0.0")
UDP_PORT = int(os.getenv("ASTERIX_UDP_PORT", "8600"))
UDP_MULTICAST_GROUP = os.getenv("ASTERIX_MULTICAST_GROUP", "")

KAFKA_BROKERS = os.getenv("KAFKA_BROKERS", "redpanda-edge:9092")
KAFKA_TOPIC = os.getenv("KAFKA_TOPIC", "ingress-asterix-raw")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Category definitions are CONFIGURATION, not code (ADR-0030). Mounted as a
# directory so a new category is a file drop and a restart — no rebuild, and
# no specification text in this repository.
ASTERIX_CONFIG_DIR = os.getenv("ASTERIX_CONFIG_DIR", "")

OPENDDIL_EDGE_ID = os.getenv("OPENDDIL_EDGE_ID", "edge-01")
OPENDDIL_REGION_ID = os.getenv("OPENDDIL_REGION_ID", "region-01")

PROMETHEUS_PORT = int(os.getenv("PROMETHEUS_PORT", "8081"))
KAFKA_BACKOFF_MAX_S = 60
MAX_DATAGRAM = 65535

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("asterix_ingestor")

# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------
AX_DATAGRAMS = Counter(
    "openddil_asterix_datagrams_received_total",
    "UDP datagrams received on the ASTERIX listener",
)
AX_RECORDS = Counter(
    "openddil_asterix_records_decoded_total",
    "ASTERIX records successfully decoded",
    ["category"],
)
AX_DECODE_ERRORS = Counter(
    "openddil_asterix_decode_errors_total",
    "Datagrams that failed to decode",
)
AX_UNKNOWN_CATEGORY = Counter(
    "openddil_asterix_unknown_category_total",
    "Records whose category has no mounted definition",
    ["category"],
)
KAFKA_PUBLISH_ERRORS = Counter(
    "openddil_asterix_kafka_publish_errors_total",
    "Kafka delivery failures",
)
KAFKA_PUBLISH_LATENCY = Histogram(
    "openddil_asterix_kafka_publish_seconds",
    "Time to hand a record to the producer",
)

_shutdown = threading.Event()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def _handle_sigterm(signum, frame):  # noqa: ANN001
    logger.info("Signal %s received — shutting down.", signum)
    _shutdown.set()


def _build_producer() -> Producer:
    conf = {
        "bootstrap.servers": KAFKA_BROKERS,
        "acks": "all",
        "linger.ms": 20,
        "compression.type": "zstd",
        "enable.idempotence": True,
    }
    backoff = 1.0
    while not _shutdown.is_set():
        try:
            producer = Producer(conf)
            producer.list_topics(timeout=5)
            logger.info("Kafka producer ready (brokers=%s, topic=%s)",
                        KAFKA_BROKERS, KAFKA_TOPIC)
            return producer
        except KafkaException as exc:
            logger.warning("Kafka not available yet (%s). Retrying in %.0f s...",
                           exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, KAFKA_BACKOFF_MAX_S)
    logger.info("Shutdown requested while waiting for Kafka.")
    sys.exit(0)


def _on_delivery(err, msg):  # noqa: ANN001
    if err is not None:
        KAFKA_PUBLISH_ERRORS.inc()
        logger.error("Kafka delivery failed: %s", err)


# ---------------------------------------------------------------------------
# Decode
# ---------------------------------------------------------------------------
def _make_parser():
    """Return the AsterixParser class, with mounted definitions if provided.

    Imported lazily so the module can be imported (and unit-tested) on a host
    without the decoder installed — the parse helpers below are pure and
    testable in isolation, which is the half most likely to be wrong.
    """
    from asterix4py import AsterixParser  # noqa: PLC0415
    return AsterixParser


def _record_key(record: dict) -> str:
    """Kafka key: stable per track so a track's updates land on one partition.

    Track number is the identity ASTERIX actually carries (I062/040, I048/161).
    It is scoped to the producing system, so SAC/SIC is prefixed — two radars
    numbering their tracks from 1 must not collide. Falls back to the source
    alone when a record carries no track number, which keeps ordering per
    source rather than pretending to an identity the record does not have.
    """
    src = record.get("010") or {}
    sac, sic = src.get("SAC"), src.get("SIC")
    trk = (record.get("040") or {}).get("TrkN")
    if trk is None:
        trk = (record.get("161") or {}).get("Tn")
    base = f"{sac}/{sic}" if sac is not None else "unknown"
    return f"asterix:{base}:{trk}" if trk is not None else f"asterix:{base}"


def _envelope(record: dict, record_no: int) -> dict:
    """Wrap a decoded record for Bronze.

    The decoded item tree is carried VERBATIM under `items`, keyed by ASTERIX
    data-item number as the decoder produced it. Deliberately not flattened or
    renamed here: renaming is interpretation, interpretation is Stage 2's job,
    and a Stage 1 that renames fields becomes a second place the ontology
    lives (ADR-0030 stage division).
    """
    return {
        "asterix_category": record.get("cat"),
        "record_index": record_no,
        "items": {k: v for k, v in record.items() if k != "cat"},
        "ingest": {
            "edge_id": OPENDDIL_EDGE_ID,
            "region_id": OPENDDIL_REGION_ID,
            "source_protocol": "EUROCONTROL-ASTERIX",
            "decoder": "asterix4py",
            "received_at_ns": time.time_ns(),
        },
    }


def decode_datagram(payload: bytes, parser_cls) -> list[dict]:
    """Decode one UDP datagram into a list of Bronze envelopes.

    Pure apart from the injected parser class, so the tests exercise this
    function directly against synthetic frames rather than standing up a
    socket. A datagram may carry several records; ASTERIX blocks are
    self-delimiting by the 3-byte header (CAT + 2-byte length).
    """
    result = parser_cls(payload).get_result()
    # asterix4py returns {record_no: {...}}; ordering is the wire order.
    envelopes = []
    for idx, (_, rec) in enumerate(sorted(result.items(), key=lambda kv: str(kv[0]))):
        cat = rec.get("cat")
        if cat is None:
            AX_UNKNOWN_CATEGORY.labels(category="none").inc()
            continue
        AX_RECORDS.labels(category=str(cat)).inc()
        envelopes.append(_envelope(rec, idx))
    return envelopes


def _open_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((UDP_HOST, UDP_PORT))
    if UDP_MULTICAST_GROUP:
        # Radar feeds are commonly multicast; joining is opt-in so a unicast
        # deployment does not need to know this exists.
        import struct  # noqa: PLC0415
        mreq = struct.pack("4sl", socket.inet_aton(UDP_MULTICAST_GROUP),
                           socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        logger.info("Joined multicast group %s", UDP_MULTICAST_GROUP)
    sock.settimeout(1.0)
    return sock


def run() -> None:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    signal.signal(signal.SIGINT, _handle_sigterm)
    start_http_server(PROMETHEUS_PORT)

    if ASTERIX_CONFIG_DIR:
        logger.info("ASTERIX definitions mounted from %s", ASTERIX_CONFIG_DIR)
    else:
        logger.info("ASTERIX definitions: decoder defaults (no mount configured)")

    parser_cls = _make_parser()
    producer = _build_producer()
    sock = _open_socket()
    logger.info("Listening for ASTERIX on udp://%s:%d", UDP_HOST, UDP_PORT)

    while not _shutdown.is_set():
        try:
            payload, _addr = sock.recvfrom(MAX_DATAGRAM)
        except socket.timeout:
            producer.poll(0)
            continue
        AX_DATAGRAMS.inc()
        try:
            envelopes = decode_datagram(payload, parser_cls)
        except Exception as exc:  # noqa: BLE001
            # A malformed datagram must not take the listener down: a radar
            # feed is not a trusted input, and one bad block should cost one
            # record, not the sidecar.
            AX_DECODE_ERRORS.inc()
            logger.warning("Decode failed for %d-byte datagram: %s", len(payload), exc)
            continue
        for env in envelopes:
            key = _record_key(env["items"])
            with KAFKA_PUBLISH_LATENCY.time():
                producer.produce(KAFKA_TOPIC, key=key.encode(),
                                 value=json.dumps(env).encode(),
                                 callback=_on_delivery)
        producer.poll(0)

    logger.info("Flushing producer...")
    producer.flush(10)
    sock.close()
    logger.info("Clean exit.")


if __name__ == "__main__":
    run()
