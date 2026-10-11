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

Two further PDU types are READ BUT NEVER PUBLISHED on their own: Electromagnetic
Emission (23) and Data (20). They feed condition.py, which resolves one
`condition` per entity (appearance, emission, health datum; worst wins) and
attaches it to that entity's next Entity State record. A record of their own
would overwrite the asset's latest state in the compacted downstream topic.

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
import re
import signal
import socket
import struct
import sys
import threading
import time

from io import BytesIO

from appearance import decode as decode_appearance
import condition

from opendis.PduFactory import createPdu
from opendis.dis7 import (
    DataPdu,
    ElectromagneticEmissionsPdu,
    EntityStatePdu,
    RemoveEntityPdu,
    EventReportPdu,
    FirePdu,
    DetonationPdu,
)

from confluent_kafka import Producer, KafkaException
from prometheus_client import Counter, Histogram
from prometheus_client.exposition import MetricsHandler

import ready
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

# A declared list of entity ids this sidecar admits, in the URN form this
# ingestor already emits (dis:<site>:<app>:<entity> -- see _entity_urn and
# _extract_entity_state's entity_id_urn field). DIS_SITE_ID answers "which
# exercise is mine"; this answers "which entities within that exercise are
# mine" -- the case where one DIS site spans two edges and both edges'
# sidecars hear every entity on it (a shared multicast group, or a unicast
# fan-out), so the site field alone can no longer tell them apart.
#
# Unset or empty is "accept every entity (subject to the site filter
# above)", so existing deployments keep today's exact behaviour and this
# stays opt-in. The actual parse happens below, once `logger` exists --
# see _parse_admitted_entity_ids, which is why DIS_ADMITTED_ENTITY_IDS
# itself is assigned there rather than here beside DIS_SITE_ID.
_ENTITY_URN_RE = re.compile(r"^dis:(\d+):(\d+):(\d+)$")

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
# Entity filter (DIS_ADMITTED_ENTITY_IDS) -- parsed here, not beside
# DIS_SITE_ID above, because a malformed list must log before refusing to
# start, and logging needs `logger` to exist first.
# ---------------------------------------------------------------------------
def _parse_admitted_entity_ids(raw: str) -> frozenset[str] | None:
    """Parse DIS_ADMITTED_ENTITY_IDS into a validated, de-duplicated set.

    Comma- and/or whitespace-separated entity URNs. Empty/unset -> None,
    meaning "no entity filter" (today's behaviour, exactly).

    Fails closed: a token that doesn't match the URN shape, or one listed
    twice, logs the offending token and exits non-zero rather than starting
    with a filter that is wrong. Silently admitting nothing (every PDU
    dropped, the sidecar looks dead) or silently admitting everything (the
    split this exists to enforce never happens) are both worse than not
    starting.
    """
    tokens = [t for t in re.split(r"[\s,]+", raw.strip()) if t]
    if not tokens:
        return None

    admitted: set[str] = set()
    for token in tokens:
        if not _ENTITY_URN_RE.match(token):
            logger.critical(
                "DIS_ADMITTED_ENTITY_IDS: malformed entity id %r -- expected "
                "dis:<site>:<app>:<entity>. Refusing to start.", token)
            sys.exit(1)
        if token in admitted:
            logger.critical(
                "DIS_ADMITTED_ENTITY_IDS: entity id %r listed twice. "
                "Refusing to start.", token)
            sys.exit(1)
        admitted.add(token)
    return frozenset(admitted)


DIS_ADMITTED_ENTITY_IDS: frozenset[str] | None = _parse_admitted_entity_ids(
    os.getenv("DIS_ADMITTED_ENTITY_IDS", ""))

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
DIS_REMOVALS_DECODED = Counter(
    "dis_removals_decoded_total",
    "Total Remove Entity PDUs (type 12) successfully decoded and published "
    "(ADR-0044 slice A)",
)
DIS_EVENT_REPORTS_DECODED = Counter(
    "dis_event_reports_decoded_total",
    "Total Event Report PDUs (type 21) successfully decoded and published "
    "(pure transport, no interpretation)",
)
DIS_EFFECTOR_PDUS_DECODED = Counter(
    "dis_effector_pdus_decoded_total",
    "Total Fire/Detonation PDUs (types 2/3) successfully decoded and "
    "published",
    ["pdu_type"],
)
DIS_PDUS_DROPPED_BY_TYPE = Counter(
    "dis_pdus_dropped_by_type_total",
    "DIS PDUs decoded but not published -- either a PDU type this sidecar "
    "does not act on, or a Remove Entity PDU that was not a single-entity "
    "claim",
    ["pdu_type"],
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
# Per-entity condition memory (arming, last emission, last health datum). In
# process, so a restart forgets it -- see condition.py.
_condition = condition.ConditionState()

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


def _appearance_decoded(pdu, zero_is_claim: bool = False) -> dict:  # noqa: ANN001
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
        zero_is_claim=zero_is_claim,
    )


def _entity_type_str(etype) -> str:  # noqa: ANN001
    """kind:domain:country:category:subcategory:specific:extra -- the key
    form of the emission baselines in dis_condition.yaml."""
    return ":".join(str(int(getattr(etype, f, 0))) for f in (
        "entityKind", "domain", "country", "category", "subcategory",
        "specific", "extra"))


def _build_entity_state_record(
    pdu: EntityStatePdu,  # noqa: ANN001
    raw_size: int,
    state: "condition.ConditionState",
    now: float | None = None,
) -> dict:
    """The Entity State record, with `condition` attached when any source claims.

    Order matters: the zero-as-claim decision reads the entity's PREVIOUS
    appearance history, so it is made before this PDU's bits are noted. The
    `appearance` key then carries the zero-as-claim decode when it applies,
    which is what lets the mapping's existing power-OFF path see it.
    `condition` is absent when no source made a claim -- absence is not health.
    """
    urn = _entity_urn(pdu.entityID)
    site = int(pdu.entityID.siteID)
    bits = _appearance_raw(pdu)
    state.note_entity_type(urn, _entity_type_str(pdu.entityType))
    zero = state.appearance_zero_is_claim(urn, site, bits)
    payload = _extract_entity_state(pdu, raw_size, zero_is_claim=zero)
    state.note_appearance(urn, site, bits, payload["appearance"], now)
    cond = state.resolve(urn, site, now)
    if cond is not None:
        payload["condition"] = cond
    # The EE for a tick follows its ES, so this carries the PREVIOUS EE's
    # state. Omitted entirely when there is no claim: silence is not a signal.
    emitting = state.emitting(urn, now)
    if emitting is not None:
        payload["emission"] = {"emitting": emitting}
    return payload


def _extract_emission_systems(pdu) -> list[dict]:  # noqa: ANN001
    """EE systems as [{"emitter_name": int, "beams": [{"erp_dbm": float}]}]."""
    systems = []
    for s in pdu.systems:
        systems.append({
            "emitter_name": int(s.emitterSystem.emitterName),
            "beams": [{"erp_dbm": float(b.fundamentalParameterData.effectiveRadiatedPower)}
                      for b in s.beamRecords],
        })
    return systems


def _accept_emission(
    pdu: "ElectromagneticEmissionsPdu",  # noqa: F821
    site_id: int | None,
    admitted: frozenset[str] | None,
) -> bool:
    """True when this EE PDU is this sidecar's to read.

    Keyed on the EMITTING entity, site then entity filter, no
    stall.note_input() -- see the call site.
    """
    if site_id is not None and int(pdu.emittingEntityID.siteID) != site_id:
        DIS_PDUS_FILTERED.labels(reason="site").inc()
        return False
    if not _entity_admitted(pdu.emittingEntityID, admitted):
        DIS_PDUS_FILTERED.labels(reason="entity").inc()
        return False
    return True


def _accept_data(
    pdu: "DataPdu",  # noqa: F821
    site_id: int | None,
    admitted: frozenset[str] | None,
) -> bool:
    """True when this Data PDU is this sidecar's to read (originating entity)."""
    if site_id is not None and int(pdu.originatingEntityID.siteID) != site_id:
        DIS_PDUS_FILTERED.labels(reason="site").inc()
        return False
    if not _entity_admitted(pdu.originatingEntityID, admitted):
        DIS_PDUS_FILTERED.labels(reason="entity").inc()
        return False
    return True


def _handle_emission(
    pdu: "ElectromagneticEmissionsPdu",  # noqa: F821
    site_id: int | None,
    admitted: frozenset[str] | None,
    state: "condition.ConditionState",
    now: float | None = None,
) -> bool:
    """Feed an accepted EE PDU into the condition state. True when accepted.

    The baseline lookup needs the entity type, which an EE does not carry; it
    comes from the last Entity State seen for the entity. None yet -> the
    state counts it and makes no claim.
    """
    if not _accept_emission(pdu, site_id, admitted):
        return False
    urn = _entity_urn(pdu.emittingEntityID)
    state.note_emission(urn, int(pdu.emittingEntityID.siteID),
                        state.entity_type(urn), _extract_emission_systems(pdu), now)
    return True


def _handle_data(
    pdu: "DataPdu",  # noqa: F821
    site_id: int | None,
    admitted: frozenset[str] | None,
    state: "condition.ConditionState",
    now: float | None = None,
) -> bool:
    """Feed an accepted Data PDU's health datum into the condition state."""
    if not _accept_data(pdu, site_id, admitted):
        return False
    datum_id = (state.cfg.get("data_health") or {}).get("datum_id")
    if datum_id is None:
        return True
    urn = _entity_urn(pdu.originatingEntityID)
    for fd in pdu.fixedDatumRecords:
        if int(fd.fixedDatumID) == int(datum_id):
            state.note_datum(urn, int(pdu.originatingEntityID.siteID),
                             int(fd.fixedDatumValue), now)
    return True


def _extract_entity_state(pdu: EntityStatePdu, raw_size: int,  # noqa: ANN001
                          zero_is_claim: bool = False) -> dict:
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
        "appearance":              _appearance_decoded(pdu, zero_is_claim),
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


def _is_single_entity_removal(receiving: "EntityID") -> bool:  # noqa: ANN001,F821
    """True only when a Remove Entity PDU's receiving EntityID names exactly
    one entity.

    DIS's wildcard convention -- entity 0xFFFF meaning "all entities [of a
    site/application]", and a wildcard site or application field extending
    that further -- lets one Remove Entity PDU name more than one entity.
    ADR-0044's two-column lifecycle model updates one row per signal and has
    no fan-out for "every entity transitioned at once", so a non-single-
    entity claim is refused here rather than guessed at. Entity 0 is also
    refused: unassigned, never a real entity in this codebase's fixtures.
    """
    return not (
        receiving.entityID in (0, 0xFFFF)
        or receiving.siteID == 0xFFFF
        or receiving.applicationID == 0xFFFF
    )


def _extract_remove_entity(pdu: "RemoveEntityPdu") -> dict:  # noqa: ANN001,F821
    """
    Extract fields from a decoded RemoveEntityPdu (type 12, Simulation
    Management family, protocol family 5) into the JSON record
    sim-dis-mapping.yaml's remove_entity branch expects (ADR-0044 slice A).

    LAYOUT AND SEMANTICS CAVEAT, same shape as the one ADR-0044 gives the
    appearance bit table: this PDU's field layout (12-byte PDU header, then
    a 6-byte originating EntityID, then a 6-byte receiving EntityID, then a
    4-byte requestID -- 28 bytes total) and the "the receiving entity is the
    one being removed" semantics are taken from Open-DIS's
    SimulationManagementFamilyPdu / RemoveEntityPdu implementation. They are
    NOT checked against the published IEEE 1278.1 text.

    Call only after _is_single_entity_removal() has confirmed the receiving
    EntityID names exactly one entity -- that is the entity this record
    claims removed.

    NOTE ON "ingest_timestamp": not part of the record shape as first
    specified for this slice, added here because sim-dis-mapping.yaml's
    remove_entity branch needs a provenance.sample_time the same way the
    Entity State branch has one ($src.ingest_timestamp) -- without it,
    sample_time and ingest_time (the mapping's own now()) would collapse
    into the same value, losing the sample-vs-ingest distinction ADR-0022/
    0023 provenance already keeps everywhere else in this pipeline.
    """
    receiving   = pdu.receivingEntityID
    originating = pdu.originatingEntityID
    entity_id_urn = f"dis:{receiving.siteID}:{receiving.applicationID}:{receiving.entityID}"

    return {
        "pdu_type": "remove_entity",
        "dis_entity_id": {
            "site":        receiving.siteID,
            "application": receiving.applicationID,
            "entity":      receiving.entityID,
        },
        "entity_id_urn": entity_id_urn,
        "originating_entity_id": {
            "site":        originating.siteID,
            "application": originating.applicationID,
            "entity":      originating.entityID,
        },
        "request_id": int(pdu.requestID),
        "ingest_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        # Same edge_id/region_id stamps _extract_entity_state puts under
        # "origin_node" (see module header) -- named "provenance" here,
        # not "origin_node", because this record has no other field a
        # mapper would confuse it with.
        "provenance": {
            "edge_id":   OPENDDIL_EDGE_ID,
            "region_id": OPENDDIL_REGION_ID,
        },
    }


# ---------------------------------------------------------------------------
# Event Report (type 21, Simulation Management family)
# ---------------------------------------------------------------------------
# PURE TRANSPORT. This sidecar reports what arrived; it does not interpret
# eventType or any datum id. What those mean is configuration that lives
# elsewhere, in a later build. dis-sim (the companion sender) follows the
# same rule — it sends what its own schedule says and knows nothing about
# what the event means either.
def _event_report_site_matches(pdu: "EventReportPdu", site_id: int | None) -> bool:  # noqa: ANN001,F821
    """True when this Event Report PDU is this sidecar's to publish.

    Mirrors the Remove Entity branch's site filter, but keyed on the
    ORIGINATING entity rather than the receiving one: Event Report has no
    removed-entity convention to borrow receivingEntityID's "entity being
    acted on" semantics from, and the originating entity is the one whose
    site this sidecar is responsible for. Pulled out as its own predicate
    (same shape as _is_single_entity_removal above) so it is unit-testable
    without a socket.
    """
    return site_id is None or int(pdu.originatingEntityID.siteID) == site_id


def _extract_event_report(pdu: "EventReportPdu") -> dict:  # noqa: ANN001,F821
    """
    Extract fields from a decoded EventReportPdu (type 21, Simulation
    Management family, protocol family 5) into a transport record.

    LAYOUT CAVEAT, same shape as _extract_remove_entity's: this PDU's field
    layout (12-byte PDU header, 6-byte originating EntityID, 6-byte
    receiving EntityID, 4-byte eventType, 4-byte padding, then the fixed/
    variable datum records) and the datum-record encoding (variableDatum
    Length counted in BITS, data padded out to the next 64-bit boundary)
    are taken from Open-DIS's SimulationManagementFamilyPdu / EventReportPdu
    / VariableDatum implementation. They are NOT checked against the
    published IEEE 1278.1 text.

    Reaches into `pdu._datums` rather than a public property: unlike its
    sibling DataPdu/SetDataPdu, this opendis version's EventReportPdu does
    not expose fixedDatumRecords/variableDatumRecords as properties --
    `_datums` (single underscore, not name-mangled) is the only access this
    library version offers.
    """
    originating = pdu.originatingEntityID
    receiving = pdu.receivingEntityID
    entity_id_urn = f"dis:{originating.siteID}:{originating.applicationID}:{originating.entityID}"

    fixed_datums = {
        str(d.fixedDatumID): int(d.fixedDatumValue)
        for d in pdu._datums.fixedDatumRecords
    }
    variable_datums = {}
    for d in pdu._datums.variableDatumRecords:
        text = bytes(d.variableData).decode("utf-8", errors="replace").rstrip("\x00")
        variable_datums[str(d.variableDatumID)] = text

    return {
        "pdu_type": "event_report",
        "dis_entity_id": {
            "site":        originating.siteID,
            "application": originating.applicationID,
            "entity":      originating.entityID,
        },
        "entity_id_urn": entity_id_urn,
        "receiving_entity_id": {
            "site":        receiving.siteID,
            "application": receiving.applicationID,
            "entity":      receiving.entityID,
        },
        "event_type": int(pdu.eventType),
        "fixed_datums": fixed_datums,
        "variable_datums": variable_datums,
        "ingest_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# Fire / Detonation (types 2 / 3, Warfare family)
# ---------------------------------------------------------------------------
# Both PDU classes inherit WarfareFamilyPdu, which is where firingEntityID
# and targetEntityID live. Detonation carries firingEntityID too (not just
# explodingEntityID) -- that is what lets both PDUs be keyed and site-
# filtered on the same field, the launcher, rather than Detonation needing
# its own convention.
def _entity_urn(entity_id) -> str:  # noqa: ANN001
    return f"dis:{entity_id.siteID}:{entity_id.applicationID}:{entity_id.entityID}"


def _entity_urn_or_none(entity_id) -> str | None:  # noqa: ANN001
    """None at the DIS wildcard-unassigned id (0:0:0), else the urn.

    Target and munition-expendable ids are optional in both PDUs -- a Fire
    with no declared target, or one with no distinct expendable (e.g. a gun
    round), sends 0:0:0 rather than omitting the field. Carrying that
    through as null rather than the literal "dis:0:0:0" keeps a downstream
    reader from mistaking "no target" for an actual entity.
    """
    if (int(entity_id.siteID) == 0
            and int(entity_id.applicationID) == 0
            and int(entity_id.entityID) == 0):
        return None
    return _entity_urn(entity_id)


def _entity_admitted(entity_id, admitted: frozenset[str] | None) -> bool:  # noqa: ANN001
    """True when `entity_id` is on the declared admit list.

    Same shape as the site filter: `admitted is None` (DIS_ADMITTED_ENTITY_IDS
    unset) accepts everything, matching DIS_SITE_ID's own default. Reuses
    _entity_urn so the comparison is against exactly the string this
    ingestor already emits as entity_id_urn, not a second formatting of the
    same three fields that could drift from it.

    Callers key this on whichever entity field that PDU's site filter uses
    (see each call site) -- not always `entity_id` on the PDU itself, e.g.
    Fire/Detonation key on firingEntityID, never targetEntityID.
    """
    return admitted is None or _entity_urn(entity_id) in admitted


def _effector_site_matches(pdu, site_id: int | None) -> bool:  # noqa: ANN001
    """True when this Fire/Detonation PDU is this sidecar's to publish.

    Keyed on firingEntityID (the launcher) for BOTH PDU types, per the
    design decision that Detonation is reported by the same site that fired
    -- not by wherever the warhead happened to land. Same mirror-of-
    _event_report_site_matches shape as that function and
    _is_single_entity_removal: a standalone predicate, unit-testable
    without a socket.
    """
    return site_id is None or int(pdu.firingEntityID.siteID) == site_id


def _munition_type_dict(descriptor) -> dict:  # noqa: ANN001
    """The DIS 7-tuple from a MunitionDescriptor.munitionType, under the
    key names the DIS 1278.1 EntityType record uses in prose (kind, domain,
    country, category, subcategory, specific, extra) rather than opendis's
    own attribute spelling (entityKind) -- this is a transport record, not
    an opendis binding leak."""
    mt = descriptor.munitionType
    return {
        "kind":        int(mt.entityKind),
        "domain":      int(mt.domain),
        "country":     int(mt.country),
        "category":    int(mt.category),
        "subcategory": int(mt.subcategory),
        "specific":    int(mt.specific),
        "extra":       int(mt.extra),
    }


def _extract_fire(pdu: "FirePdu") -> dict:  # noqa: ANN001,F821
    """
    Extract fields from a decoded FirePdu (type 2, Warfare family) into a
    transport record. PURE TRANSPORT, same discipline as _extract_event_
    report: no interpretation of munitionType, warhead or fuse values --
    those are opaque DIS codes here.
    """
    event = pdu.eventID
    sim_addr = event.simulationAddress
    descriptor = pdu.descriptor
    loc = pdu.location

    return {
        "pdu_type": "fire",
        "event_urn": f"dis-event:{sim_addr.site}:{sim_addr.application}:{event.eventNumber}",
        "launcher_urn": _entity_urn(pdu.firingEntityID),
        "target_urn": _entity_urn_or_none(pdu.targetEntityID),
        "munition_urn": _entity_urn_or_none(pdu.munitionExpendableID),
        "munition_type": _munition_type_dict(descriptor),
        "quantity": int(descriptor.quantity),
        "warhead": int(descriptor.warhead),
        "fuse": int(descriptor.fuse),
        "range": float(pdu.range),
        "location": {"x": loc.x, "y": loc.y, "z": loc.z},
        "ingest_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        # Same edge_id/region_id stamp _extract_remove_entity carries --
        # this is the point of origin for the field, not a later Connect-
        # layer mapping, since this process is the one that knows its own
        # identity (the env vars below) and downstream mappings only
        # forward what is already here.
        "provenance": {
            "edge_id":   OPENDDIL_EDGE_ID,
            "region_id": OPENDDIL_REGION_ID,
        },
    }


# ---------------------------------------------------------------------------
# Resupply Received (type 7, Logistics family)
# ---------------------------------------------------------------------------
# Hand-decoded with struct: opendis's parse for this PDU raises whenever
# supplies are present, so createPdu is never called for type 7. Layout is
# big-endian, offsets from the start of the datagram: 12-byte header, then
# receivingEntityID (6), supplyingEntityID (6), numberOfSupplyTypes (1),
# padding, then n 12-byte records (8-byte EntityType + float32 quantity).
def _id_urn(site: int, application: int, entity: int) -> str:
    """Same string _entity_urn produces, from plain ints."""
    return f"dis:{site}:{application}:{entity}"


def _parse_resupply_received(data: bytes) -> dict:
    """
    Decode a Resupply Received PDU (type 7) into a transport record. PURE
    TRANSPORT, same discipline as _extract_fire: supply types and
    quantities are carried as-is. The padding width is derived from the
    declared length (3 or 4 bytes accepted); anything else, or a datagram
    shorter than its declared length, raises ValueError.
    """
    if len(data) < 25:
        raise ValueError(f"resupply received too short: {len(data)} bytes")
    timestamp, length = struct.unpack_from(">IH", data, 4)
    rs, ra, re_, ss, sa, se, n = struct.unpack_from(">HHHHHHB", data, 12)
    if len(data) < length:
        raise ValueError(f"resupply received truncated: {len(data)} < {length}")
    pad = length - 25 - 12 * n
    if pad not in (3, 4):
        raise ValueError(f"resupply received bad padding {pad} "
                         f"(length={length}, supplies={n})")
    supplies = []
    off = 25 + pad
    for _ in range(n):
        kind, domain, country, cat, sub, spec, extra, qty = struct.unpack_from(
            ">BBHBBBBf", data, off)
        supplies.append({
            "munition_type": {
                "kind": kind, "domain": domain, "country": country,
                "category": cat, "subcategory": sub, "specific": spec,
                "extra": extra,
            },
            "quantity": float(qty),
        })
        off += 12
    return {
        "pdu_type": "resupply_received",
        "event_urn": f"dis-resupply:{rs}:{ra}:{re_}:{timestamp}",
        "launcher_urn": _id_urn(rs, ra, re_),
        "supplier_urn": _id_urn(ss, sa, se),
        "supplies": supplies,
        "ingest_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "provenance": {
            "edge_id":   OPENDDIL_EDGE_ID,
            "region_id": OPENDDIL_REGION_ID,
        },
    }


def _extract_detonation(pdu: "DetonationPdu") -> dict:  # noqa: ANN001,F821
    """
    Extract fields from a decoded DetonationPdu (type 3, Warfare family)
    into a transport record. `detonation_result` is carried as the raw DIS
    enum int -- no mapping to a terminal-state vocabulary in this phase.
    """
    event = pdu.eventID
    sim_addr = event.simulationAddress
    descriptor = pdu.descriptor
    loc = pdu.location

    return {
        "pdu_type": "detonation",
        "event_urn": f"dis-event:{sim_addr.site}:{sim_addr.application}:{event.eventNumber}",
        "launcher_urn": _entity_urn(pdu.firingEntityID),
        "target_urn": _entity_urn_or_none(pdu.targetEntityID),
        "munition_type": _munition_type_dict(descriptor),
        "quantity": int(descriptor.quantity),
        "warhead": int(descriptor.warhead),
        "fuse": int(descriptor.fuse),
        "detonation_result": int(pdu.detonationResult),
        "location": {"x": loc.x, "y": loc.y, "z": loc.z},
        "ingest_timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "provenance": {
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
    """/metrics plus /healthz/live and /healthz/ready, on the port the chart
    already scrapes.

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
            if self.path.startswith("/healthz/ready"):
                rd = ready.state()
                body = json.dumps(rd).encode()
                # 503 here is what pulls the pod out of the Service's
                # endpoints -- unlike /healthz/live's 503, which triggers a
                # kubelet restart.
                self.send_response(200 if rd["ready"] else 503)
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


def _open_multicast_socket(group: str, iface: str, port: int, bufsize: int) -> socket.socket:
    """Bind the wildcard and join `group`, with both reuse options set.

    This sidecar runs in the same pod as a DIS simulator, both wanting the
    same multicast group and port, no hostNetwork between them. Linux grants
    a second bind on a port only when every socket on it sets SO_REUSEADDR,
    or every socket sets SO_REUSEPORT and runs as the same effective uid --
    and several sidecars are MEANT to sit on one group and one port, each
    taking its own site, so this is not boilerplate here either way. The
    co-located simulator may set only one of the two, not both, so this
    process sets both itself rather than hope the other side picked the same
    one; multicast is delivered to every bound socket regardless of which
    mechanism let the bind through. Without either, the second process to
    start fails to bind and the deployment silently has one fewer ingester
    than it was told to have.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, bufsize)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Not every platform has SO_REUSEPORT (Windows does not) -- set it
    # only where it exists rather than fail the whole bind over it.
    if hasattr(socket, "SO_REUSEPORT"):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    # Bind the wildcard, not the group: binding the group address is
    # accepted on Linux but not portable, and the wildcard is what lets
    # the same container also answer a unicast probe on this port.
    sock.bind(("0.0.0.0", port))
    mreq = socket.inet_aton(group) + socket.inet_aton(iface)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    return sock


def _accept_entity_state(
    pdu: EntityStatePdu,
    site_id: int | None,
    admitted: frozenset[str] | None = None,
) -> bool:
    """True when this PDU is this sidecar's to publish.

    Extracted so the accept decision is callable without the socket loop
    around it. The site filter runs first, then the entity filter, and
    either short-circuits on a miss -- see the comment at the call site in
    run() for why that ordering is load-bearing for the stall detector.
    Readiness takes the same signal: an accepted Entity State PDU is "the
    simulator is alive and this sidecar is seeing one of its own entities,"
    which is what /healthz/ready reports.
    """
    if site_id is not None and int(pdu.entityID.siteID) != site_id:
        DIS_PDUS_FILTERED.labels(reason="site").inc()
        return False
    if not _entity_admitted(pdu.entityID, admitted):
        DIS_PDUS_FILTERED.labels(reason="entity").inc()
        return False
    stall.note_input()
    ready.note_entity()
    return True


def _accept_remove_entity(
    pdu: "RemoveEntityPdu",  # noqa: F821
    site_id: int | None,
    admitted: frozenset[str] | None,
) -> bool:
    """True when this Remove Entity PDU is this sidecar's to publish.

    Keyed on the RECEIVING entity -- that is the entity being removed, so
    it is the entity whose site/admission decides whether this is this
    sidecar's input. Same site-then-entity ordering as every other accept
    function here; either miss increments DIS_PDUS_FILTERED and returns
    False. Unlike _accept_entity_state, this does NOT call
    stall.note_input() -- the call site does that itself, after a True, so
    the ordering (before note_input()) is visible at the one place that
    matters instead of hidden inside the predicate.
    """
    if site_id is not None and int(pdu.receivingEntityID.siteID) != site_id:
        DIS_PDUS_FILTERED.labels(reason="site").inc()
        return False
    if not _entity_admitted(pdu.receivingEntityID, admitted):
        DIS_PDUS_FILTERED.labels(reason="entity").inc()
        return False
    return True


def _accept_effector(
    pdu,  # noqa: ANN001
    site_id: int | None,
    admitted: frozenset[str] | None,
) -> bool:
    """True when this Fire/Detonation PDU is this sidecar's to publish.

    Keyed on firingEntityID (the launcher) for BOTH PDU types, never
    targetEntityID -- see _effector_site_matches's docstring for why.
    Same site-then-entity ordering and no stall.note_input() call, same as
    _accept_remove_entity above.
    """
    if not _effector_site_matches(pdu, site_id):
        DIS_PDUS_FILTERED.labels(reason="site").inc()
        return False
    if not _entity_admitted(pdu.firingEntityID, admitted):
        DIS_PDUS_FILTERED.labels(reason="entity").inc()
        return False
    return True


def _accept_resupply_received(
    payload: dict,
    site_id: int | None,
    admitted: frozenset[str] | None,
) -> bool:
    """True when this Resupply Received record is this sidecar's to publish.

    Keyed on the RECEIVER (launcher_urn, the entity being refilled). Same
    site-then-entity ordering, reasons and no stall.note_input() call as
    _accept_effector.
    """
    urn = payload["launcher_urn"]
    if site_id is not None and int(urn.split(":")[1]) != site_id:
        DIS_PDUS_FILTERED.labels(reason="site").inc()
        return False
    if admitted is not None and urn not in admitted:
        DIS_PDUS_FILTERED.labels(reason="entity").inc()
        return False
    return True


def _accept_event_report(
    pdu: "EventReportPdu",  # noqa: F821
    site_id: int | None,
    admitted: frozenset[str] | None,
) -> bool:
    """True when this Event Report PDU is this sidecar's to publish.

    Keyed on the ORIGINATING entity -- see _event_report_site_matches's
    docstring for why that differs from the Remove Entity branch's
    receiving-entity key. Same site-then-entity ordering and no
    stall.note_input() call, same as _accept_remove_entity above.
    """
    if not _event_report_site_matches(pdu, site_id):
        DIS_PDUS_FILTERED.labels(reason="site").inc()
        return False
    if not _entity_admitted(pdu.originatingEntityID, admitted):
        DIS_PDUS_FILTERED.labels(reason="entity").inc()
        return False
    return True


def run():
    global _producer  # noqa: PLW0603

    # Metrics AND the liveness endpoint on one server. The stall state lives
    # in-process because the component already knows its own progress; see
    # stall.py for why this gates LIVENESS rather than readiness.
    _serve_http()
    logger.info("Prometheus /metrics and /healthz/live, /healthz/ready listening on :%d",
                PROMETHEUS_PORT)

    # Connect to Kafka (blocks until ready or shutdown)
    _producer = _build_producer()

    # Start stats logger thread
    stats_thread = threading.Thread(target=_stats_logger, daemon=True)
    stats_thread.start()

    # Open UDP socket
    if DIS_MULTICAST_GROUP:
        sock = _open_multicast_socket(DIS_MULTICAST_GROUP, DIS_MULTICAST_IFACE,
                                       UDP_PORT, UDP_BUFSIZE)
        logger.info(
            "Joined multicast group %s on iface %s, listening UDP :%d "
            "(SO_RCVBUF=%d MB)",
            DIS_MULTICAST_GROUP, DIS_MULTICAST_IFACE, UDP_PORT,
            UDP_BUFSIZE // (1024 * 1024),
        )
    else:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UDP_BUFSIZE)
        sock.bind((UDP_HOST, UDP_PORT))
        logger.info(
            "Listening on UDP %s:%d (unicast, SO_RCVBUF=%d MB)",
            UDP_HOST, UDP_PORT, UDP_BUFSIZE // (1024 * 1024),
        )

    # Readiness's start time is "the socket is open," not module import --
    # see ready.py's note_started() for why the import-time default is only
    # a fallback.
    ready.note_started()

    sock.settimeout(1.0)  # Non-blocking so we can honour _shutdown

    # Say the scope out loud at startup. An operator reading one line of log
    # should be able to tell a sidecar that is ignoring most of the feed on
    # purpose from one that is not receiving it.
    if DIS_SITE_ID is None:
        logger.info("Site filter: OFF -- publishing every site that arrives")
    else:
        logger.info("Site filter: site %d ONLY -- all other sites counted "
                    "in dis_pdus_filtered_total and dropped", DIS_SITE_ID)

    if DIS_ADMITTED_ENTITY_IDS is None:
        logger.info("Entity filter: OFF")
    else:
        logger.info("Entity filter: %d ids ONLY -- %s",
                    len(DIS_ADMITTED_ENTITY_IDS),
                    ", ".join(sorted(DIS_ADMITTED_ENTITY_IDS)))

    try:
        while not _shutdown.is_set():
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                _producer.poll(0)  # Service delivery callbacks
                continue

            # --- Resupply Received (type 7): hand-decoded, never createPdu ---
            if len(data) >= 12 and data[2] == 7:
                DIS_PDUS_RECEIVED.labels(pdu_type="7").inc()
                try:
                    payload = _parse_resupply_received(data)
                except Exception as exc:
                    DIS_DECODE_ERRORS.inc()
                    logger.debug("Resupply decode error from %s: %s", addr, exc)
                    continue
                if not _accept_resupply_received(payload, DIS_SITE_ID,
                                                 DIS_ADMITTED_ENTITY_IDS):
                    continue
                stall.note_input()
                DIS_EFFECTOR_PDUS_DECODED.labels(pdu_type="resupply_received").inc()
            else:
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

                # --- Filter: only PDU types this sidecar understands ---
                # Entity State (type 1, unchanged path), Remove Entity (type
                # 12, ADR-0044 slice A), Event Report (type 21), and Fire /
                # Detonation (types 2 / 3, effector events). Data (20) and
                # Electromagnetic Emission (23) are read into the condition
                # state and publish nothing themselves. Anything else is
                # decoded fine by opendis but is not a signal this sidecar acts
                # on, so it is dropped and counted rather than silently
                # discarded -- dis_pdus_received_total{pdu_type=...} already
                # saw it arrive; this counter says what happened to it next.
                if pdu_type == 1:
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
                    if not _accept_entity_state(pdu, DIS_SITE_ID, DIS_ADMITTED_ENTITY_IDS):
                        continue

                    # --- Extract to JSON ---
                    try:
                        payload = _build_entity_state_record(pdu, len(data), _condition)
                    except Exception as exc:
                        DIS_DECODE_ERRORS.inc()
                        logger.warning("Field extraction error from %s: %s", addr, exc)
                        continue

                elif pdu_type == 12:
                    if not isinstance(pdu, RemoveEntityPdu):
                        DIS_DECODE_ERRORS.inc()
                        logger.debug("PDU type=12 but not RemoveEntityPdu from %s — dropping", addr)
                        continue

                    # --- Not a single-entity claim: drop and count ---
                    #
                    # DIS's wildcard convention -- receiving entity 0xFFFF
                    # meaning "all entities [of a site/application]", and by
                    # extension a wildcard site or application field -- lets one
                    # Remove Entity PDU name more than one entity. ADR-0044's
                    # two-column lifecycle model updates one row per signal; it
                    # has no fan-out for "every entity transitioned at once", so
                    # rather than guess one, a non-single-entity claim is
                    # dropped and counted here. Receiving entity 0 is also
                    # refused -- EntityID 0 is unassigned, never a real entity
                    # in this codebase's fixtures.
                    if not _is_single_entity_removal(pdu.receivingEntityID):
                        DIS_PDUS_DROPPED_BY_TYPE.labels(pdu_type=str(pdu_type)).inc()
                        logger.debug(
                            "Dropped Remove Entity PDU from %s (not a single-entity "
                            "claim: receiving=%d:%d:%d)", addr,
                            pdu.receivingEntityID.siteID,
                            pdu.receivingEntityID.applicationID,
                            pdu.receivingEntityID.entityID,
                        )
                        continue

                    # --- Filter: site, then declared entities ---
                    # Same rationale as the Entity State path above (see that
                    # block's comment), keyed on the RECEIVING entity -- that
                    # is the entity being removed, so it is the entity whose
                    # site/admission decides whether this is this sidecar's
                    # input. Same ordering rule as every path here: before
                    # stall.note_input(), so a Remove Entity PDU this sidecar
                    # didn't declare does not count as this sidecar's input.
                    if not _accept_remove_entity(pdu, DIS_SITE_ID, DIS_ADMITTED_ENTITY_IDS):
                        continue

                    stall.note_input()
                    DIS_REMOVALS_DECODED.inc()

                    # --- Extract to JSON ---
                    try:
                        payload = _extract_remove_entity(pdu)
                    except Exception as exc:
                        DIS_DECODE_ERRORS.inc()
                        logger.warning("Field extraction error from %s: %s", addr, exc)
                        continue

                elif pdu_type in (2, 3):
                    expected_cls = FirePdu if pdu_type == 2 else DetonationPdu
                    if not isinstance(pdu, expected_cls):
                        DIS_DECODE_ERRORS.inc()
                        logger.debug("PDU type=%d but not %s from %s — dropping",
                                     pdu_type, expected_cls.__name__, addr)
                        continue

                    # --- Filter: site, then declared entities ---
                    # Keyed on firingEntityID (the launcher) for both PDU
                    # types, never targetEntityID -- see _effector_site_
                    # matches's docstring. Same before-stall.note_input()
                    # ordering as every path here.
                    if not _accept_effector(pdu, DIS_SITE_ID, DIS_ADMITTED_ENTITY_IDS):
                        continue

                    stall.note_input()

                    # --- Extract to JSON ---
                    try:
                        if pdu_type == 2:
                            payload = _extract_fire(pdu)
                        else:
                            payload = _extract_detonation(pdu)
                    except Exception as exc:
                        DIS_DECODE_ERRORS.inc()
                        logger.warning("Field extraction error from %s: %s", addr, exc)
                        continue

                    DIS_EFFECTOR_PDUS_DECODED.labels(pdu_type=payload["pdu_type"]).inc()

                elif pdu_type == 21:
                    if not isinstance(pdu, EventReportPdu):
                        DIS_DECODE_ERRORS.inc()
                        logger.debug("PDU type=21 but not EventReportPdu from %s — dropping", addr)
                        continue

                    # --- Filter: site, then declared entities ---
                    # Keyed on the ORIGINATING entity -- see _event_report_
                    # site_matches's docstring for why that differs from the
                    # Remove Entity branch's receiving-entity key. Same
                    # before-stall.note_input() ordering as every path here.
                    if not _accept_event_report(pdu, DIS_SITE_ID, DIS_ADMITTED_ENTITY_IDS):
                        continue

                    stall.note_input()
                    DIS_EVENT_REPORTS_DECODED.inc()

                    # --- Extract to JSON ---
                    try:
                        payload = _extract_event_report(pdu)
                    except Exception as exc:
                        DIS_DECODE_ERRORS.inc()
                        logger.warning("Field extraction error from %s: %s", addr, exc)
                        continue

                elif pdu_type in (20, 23):
                    expected_cls = DataPdu if pdu_type == 20 else ElectromagneticEmissionsPdu
                    if not isinstance(pdu, expected_cls):
                        DIS_DECODE_ERRORS.inc()
                        logger.debug("PDU type=%d but not %s from %s — dropping",
                                     pdu_type, expected_cls.__name__, addr)
                        continue

                    # Read into the condition state and publish NOTHING: the
                    # claim rides on the entity's next Entity State record
                    # (see condition.py). Same site-then-entity filters as ES.
                    #
                    # Neither calls stall.note_input(). The stall rule is
                    # "input advanced while output did not" (stall.py), and
                    # these produce no output record of their own, so counting
                    # them as input would make a healthy sidecar that is
                    # receiving only EE/Data between Entity States look
                    # stalled -- the ES block's comment above explains the
                    # same trap for other sites' traffic.
                    if pdu_type == 23:
                        _handle_emission(pdu, DIS_SITE_ID, DIS_ADMITTED_ENTITY_IDS, _condition)
                    else:
                        _handle_data(pdu, DIS_SITE_ID, DIS_ADMITTED_ENTITY_IDS, _condition)
                    continue

                else:
                    DIS_PDUS_DROPPED_BY_TYPE.labels(pdu_type=str(pdu_type)).inc()
                    logger.debug("Dropped PDU type %d from %s (not Entity State, "
                                 "Remove Entity, Event Report, Fire, "
                                 "Detonation, Resupply Received, Data, or "
                                 "Electromagnetic Emission)", pdu_type, addr)
                    continue

            # Fire/Detonation records carry launcher_urn, not entity_id_urn
            # (an event id is never an entity id -- see event_urn's
            # dis-event: prefix). Every other record shape keys on
            # entity_id_urn as before.
            key = payload.get("entity_id_urn") or payload.get("launcher_urn")
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
