"""
Regenerate the binary DIS fixtures used by Hero Scenario v3.

The previous incarnation of this script wrote a hand-curated hex blob that
opendis 1.0 could NOT parse (createPdu returned mid-stream EOF). This
version uses opendis's own serializer so the byte layout is guaranteed to
round-trip through opendis.PduFactory.createPdu().

Run: `python generate_fixtures.py` from this directory.

Verified 2026-05-12 against opendis==1.0:
  sample_entity_state.bin : 144 bytes (Entity State PDU type=1, M1A2 SEPv3)
  sample_fire_pdu.bin     :  96 bytes (Fire PDU type=2)
  sample_malformed.bin    :  32 bytes (random noise from os.urandom)
"""
from __future__ import annotations

import os
from io import BytesIO
from pathlib import Path

from opendis.dis7 import EntityStatePdu, FirePdu
from opendis.DataOutputStream import DataOutputStream
from opendis.PduFactory import createPdu

OUT_DIR = Path(__file__).parent


def _serialize(pdu) -> bytes:
    bio = BytesIO()
    pdu.serialize(DataOutputStream(bio))
    return bio.getvalue()


def _save(data: bytes, name: str) -> None:
    path = OUT_DIR / name
    path.write_bytes(data)
    print(f"WROTE {name} ({len(data)} bytes)")


# ---------------------------------------------------------------------------
# Entity State PDU — M1A2 SEPv3
# Triplet (1,1,225,1,3,1,0) per dis_entity_types.yaml
# ---------------------------------------------------------------------------
def make_entity_state() -> bytes:
    pdu = EntityStatePdu()
    pdu.protocolVersion = 7
    pdu.exerciseID      = 1
    pdu.pduType         = 1   # Entity State
    pdu.protocolFamily  = 1   # Entity Information / Interaction
    pdu.pduStatus       = 0
    pdu.entityAppearance = 0
    pdu.capabilities    = 0

    pdu.entityID.siteID        = 1
    pdu.entityID.applicationID = 1
    pdu.entityID.entityID      = 4773

    pdu.entityType.entityKind  = 1
    pdu.entityType.domain      = 1
    pdu.entityType.country     = 225
    pdu.entityType.category    = 1
    pdu.entityType.subcategory = 3
    pdu.entityType.specific    = 1
    pdu.entityType.extra       = 0

    pdu.forceId = 1
    pdu.marking.characters = list(b"IRON-01".ljust(11, b"\x00"))

    return _serialize(pdu)


# ---------------------------------------------------------------------------
# Fire PDU — for the "drop and count" test
# ---------------------------------------------------------------------------
def make_fire_pdu() -> bytes:
    pdu = FirePdu()
    pdu.protocolVersion = 7
    pdu.exerciseID      = 1
    pdu.pduType         = 2   # Fire
    pdu.protocolFamily  = 2   # Warfare
    pdu.pduStatus       = 0
    pdu.fireMissionIndex = 0
    pdu.range = 0.0
    return _serialize(pdu)


# ---------------------------------------------------------------------------
# Malformed — random noise
# ---------------------------------------------------------------------------
def make_malformed() -> bytes:
    return os.urandom(32)


# ---------------------------------------------------------------------------
# Build + verify
# ---------------------------------------------------------------------------
def _verify(data: bytes, expected_type: int | None, label: str) -> None:
    """Assert the round-trip parses (or doesn't, for malformed)."""
    try:
        p = createPdu(data)
    except Exception as exc:
        if expected_type is None:
            print(f"  OK   {label}: opendis rejected as expected ({exc.__class__.__name__})")
            return
        raise

    if expected_type is None:
        if p is None:
            print(f"  OK   {label}: opendis returned None as expected")
        else:
            print(f"  WARN {label}: opendis unexpectedly parsed as {type(p).__name__}")
        return

    if p is None:
        raise RuntimeError(f"{label}: expected pdu type {expected_type}, got None")
    if p.pduType != expected_type:
        raise RuntimeError(
            f"{label}: expected pdu type {expected_type}, got {p.pduType}"
        )
    print(f"  OK   {label}: round-tripped via opendis (type={p.pduType})")


def main() -> None:
    print("Generating fixtures...")

    es = make_entity_state()
    _save(es, "sample_entity_state.bin")
    _verify(es, expected_type=1, label="sample_entity_state.bin")

    fp = make_fire_pdu()
    _save(fp, "sample_fire_pdu.bin")
    _verify(fp, expected_type=2, label="sample_fire_pdu.bin")

    mal = make_malformed()
    _save(mal, "sample_malformed.bin")
    _verify(mal, expected_type=None, label="sample_malformed.bin")

    print("Done.")


if __name__ == "__main__":
    main()
