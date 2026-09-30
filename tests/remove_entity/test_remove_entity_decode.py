"""
Unit tests for the Remove Entity (PDU type 12) decode path — ADR-0044
slice A.

PURE UNIT TESTS, NO INFRASTRUCTURE. Unlike tests/multicast_site_filter,
which drives real sidecar containers over docker compose, these tests call
dis_ingestor's decode helpers directly against fabricated bytes. No socket,
no Kafka, no compose stack — they import the module and check what it
returns.

Byte fabrication strategy, and why it differs by PDU:

  * Remove Entity fixtures (valid, ALL_ENTITIES, short) are built with
    struct.pack directly against the 28-byte layout
    _extract_remove_entity's docstring documents: 12-byte PDU header, 6-byte
    originating EntityID, 6-byte receiving EntityID, 4-byte requestID. That
    layout is Open-DIS's, not verified against the published IEEE 1278.1
    text (see the docstring caveat) — these tests pin THIS decoder's
    behaviour against THAT layout, not against the standard.

  * The Entity State regression fixture is built by constructing a real
    opendis EntityStatePdu object and calling its own .serialize(), rather
    than hand-packing ~15 nested fields (dead-reckoning parameters, marking,
    variable parameters, ...). It still exercises the real wire bytes
    through the real createPdu() decode path; it just lets opendis do the
    encoding side so this file isn't 100 lines of unrelated struct layout.

Run with: python -m unittest discover -s tests/remove_entity
(or via pytest, if available — no pytest-only features are used).
"""
from __future__ import annotations

import struct
import sys
import unittest
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from opendis.DataOutputStream import DataOutputStream  # noqa: E402
from opendis.PduFactory import createPdu  # noqa: E402
from opendis.dis7 import EntityStatePdu, RemoveEntityPdu  # noqa: E402

import dis_ingestor  # noqa: E402


# ---------------------------------------------------------------------------
# Remove Entity byte fabrication
# ---------------------------------------------------------------------------
def _remove_entity_bytes(
    originating: tuple[int, int, int],
    receiving: tuple[int, int, int],
    request_id: int,
    *,
    truncate_to: int | None = None,
) -> bytes:
    """Hand-pack a Remove Entity PDU (type 12).

    Layout (28 bytes, big-endian — see _extract_remove_entity's docstring
    for the Open-DIS-not-IEEE-1278.1 caveat):
      header:              B B B B I H B B   (12 bytes)
      originatingEntityID: H H H             (6 bytes: site, app, entity)
      receivingEntityID:   H H H             (6 bytes: site, app, entity)
      requestID:           I                 (4 bytes)
    """
    header = struct.pack(
        ">BBBBIHBB",
        7,   # protocolVersion (DIS-2009)
        1,   # exerciseID
        12,  # pduType — Remove Entity
        5,   # protocolFamily — Simulation Management
        0,   # timestamp
        28,  # length
        0,   # pduStatus
        0,   # padding
    )
    body = (
        struct.pack(">HHH", *originating)
        + struct.pack(">HHH", *receiving)
        + struct.pack(">I", request_id)
    )
    data = header + body
    if truncate_to is not None:
        data = data[:truncate_to]
    return data


class RemoveEntityDecodeTests(unittest.TestCase):
    def test_valid_remove_entity_decodes_and_extracts(self):
        data = _remove_entity_bytes(
            originating=(1, 1, 0),
            receiving=(1, 1, 1005),
            request_id=42,
        )
        pdu = createPdu(data)

        self.assertIsInstance(pdu, RemoveEntityPdu)
        self.assertEqual(int(pdu.pduType), 12)
        self.assertTrue(dis_ingestor._is_single_entity_removal(pdu.receivingEntityID))

        payload = dis_ingestor._extract_remove_entity(pdu)

        self.assertEqual(payload["pdu_type"], "remove_entity")
        self.assertEqual(
            payload["dis_entity_id"],
            {"site": 1, "application": 1, "entity": 1005},
        )
        self.assertEqual(payload["entity_id_urn"], "dis:1:1:1005")
        self.assertEqual(
            payload["originating_entity_id"],
            {"site": 1, "application": 1, "entity": 0},
        )
        self.assertEqual(payload["request_id"], 42)
        self.assertIn("ingest_timestamp", payload)
        self.assertEqual(
            payload["provenance"],
            {
                "edge_id": dis_ingestor.OPENDDIL_EDGE_ID,
                "region_id": dis_ingestor.OPENDDIL_REGION_ID,
            },
        )
        # Not the ESPDU record shape — no kinematics/appearance keys leaked
        # in from the wrong extraction path.
        self.assertNotIn("appearance", payload)
        self.assertNotIn("location_ecef", payload)

    def test_all_entities_is_not_a_single_entity_claim(self):
        # entity field 0xFFFF — DIS's "all entities [of that site/app]"
        # wildcard.
        data = _remove_entity_bytes(
            originating=(1, 1, 0),
            receiving=(1, 1, 0xFFFF),
            request_id=7,
        )
        pdu = createPdu(data)
        self.assertIsInstance(pdu, RemoveEntityPdu)
        self.assertFalse(dis_ingestor._is_single_entity_removal(pdu.receivingEntityID))

    def test_entity_zero_is_not_a_single_entity_claim(self):
        data = _remove_entity_bytes(
            originating=(1, 1, 0),
            receiving=(1, 1, 0),
            request_id=8,
        )
        pdu = createPdu(data)
        self.assertFalse(dis_ingestor._is_single_entity_removal(pdu.receivingEntityID))

    def test_wildcard_site_or_application_is_not_a_single_entity_claim(self):
        wildcard_site = _remove_entity_bytes(
            originating=(1, 1, 0), receiving=(0xFFFF, 1, 1005), request_id=9,
        )
        wildcard_app = _remove_entity_bytes(
            originating=(1, 1, 0), receiving=(1, 0xFFFF, 1005), request_id=10,
        )
        self.assertFalse(
            dis_ingestor._is_single_entity_removal(createPdu(wildcard_site).receivingEntityID)
        )
        self.assertFalse(
            dis_ingestor._is_single_entity_removal(createPdu(wildcard_app).receivingEntityID)
        )

    def test_short_pdu_raises_rather_than_returning_garbage(self):
        # Truncated mid-way through receivingEntityID (20 of 28 bytes). The
        # production path (dis_ingestor.run()'s main loop) wraps createPdu()
        # in a try/except that counts this under DIS_DECODE_ERRORS and drops
        # it — see the module's PDU-decode block. This test pins the
        # decoder-level behaviour that path relies on: a short PDU raises
        # rather than silently returning a partially-populated object.
        data = _remove_entity_bytes(
            originating=(1, 1, 0),
            receiving=(1, 1, 1005),
            request_id=42,
            truncate_to=20,
        )
        with self.assertRaises(Exception):
            createPdu(data)


# ---------------------------------------------------------------------------
# Entity State (type 1) regression — the record shape must not change
# ---------------------------------------------------------------------------
class EntityStateUnchangedTests(unittest.TestCase):
    def test_espdu_record_shape_unchanged(self):
        pdu = EntityStatePdu()
        # opendis's PduSuperclass.__init__ defaults pduType=0 and every
        # subclass's __init__ calls super().__init__() bare, so the class
        # attribute pduType=1 declared on EntityStatePdu never reaches the
        # instance — self.pduType is 0 until set explicitly. Harmless for
        # real traffic (createPdu reads the wire byte, not this attribute),
        # but this fixture serializes the object itself, so it must be set
        # or the round-trip produces a PDU that decodes as pduType 0.
        pdu.pduType = 1
        pdu.entityID.siteID = 1
        pdu.entityID.applicationID = 1
        pdu.entityID.entityID = 2000
        # opendis's own default (a PduStatus object) doesn't survive its own
        # serialize() — Pdu.serialize() writes it with write_unsigned_byte(),
        # which requires an int. Not our decoder's bug; pin it to a plain
        # int so this fixture can round-trip.
        pdu.pduStatus = 0
        # Same story as pduStatus: opendis defaults entityAppearance and
        # capabilities to b'0000' (bytes), which its own serialize() cannot
        # pack as an unsigned int. Pin both to plain ints.
        pdu.entityAppearance = 0
        pdu.capabilities = 0

        buf = BytesIO()
        pdu.serialize(DataOutputStream(buf))
        data = buf.getvalue()

        decoded = createPdu(data)
        self.assertIsInstance(decoded, EntityStatePdu)

        payload = dis_ingestor._extract_entity_state(decoded, len(data))

        # The shape this record has always had.
        self.assertEqual(payload["entity_id_urn"], "dis:1:1:2000")
        self.assertIn("origin_node", payload)
        self.assertIn("appearance", payload)
        self.assertIn("location_ecef", payload)

        # What ADR-0044 slice A must NOT have added to this path. (Note:
        # "dis_entity_id" is already a legitimate ESPDU key — it is not one
        # of these.)
        self.assertNotIn("pdu_type", payload)
        self.assertNotIn("provenance", payload)
        self.assertNotIn("originating_entity_id", payload)
        self.assertNotIn("request_id", payload)


if __name__ == "__main__":
    unittest.main()
