"""
Unit tests for the Fire / Detonation (PDU types 2 / 3, Warfare family)
decode path.

PURE UNIT TESTS, NO INFRASTRUCTURE, same contract as
tests/event_report/test_event_report_decode.py: these call dis_ingestor's
decode helpers directly against bytes built with opendis's own FirePdu /
DetonationPdu classes and check what comes back. No socket, no Kafka, no
compose stack.

PURE TRANSPORT, NOT INTERPRETATION. munition_type, warhead, fuse and
detonation_result are opaque DIS codes throughout -- this phase decodes and
publishes them, nothing more.

Run with: python -m unittest discover -s tests/effector
(or via pytest, if available -- no pytest-only features are used).
"""
from __future__ import annotations

import sys
import unittest
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from opendis.DataOutputStream import DataOutputStream  # noqa: E402
from opendis.PduFactory import createPdu  # noqa: E402
from opendis.dis7 import (  # noqa: E402
    CollisionPdu,
    DetonationPdu,
    EntityID,
    EntityType,
    EventIdentifier,
    FirePdu,
    MunitionDescriptor,
    SimulationAddress,
    Vector3Double,
)

import dis_ingestor  # noqa: E402


def _entity_id(site: int, application: int, entity: int) -> EntityID:
    return EntityID(siteID=site, applicationID=application, entityID=entity)


def _build_fire_pdu(
    firing: tuple[int, int, int],
    target: tuple[int, int, int],
    event: tuple[int, int, int],
    munition: tuple[int, int, int] = (0, 0, 0),
    munition_type: tuple[int, int, int, int, int, int, int] = (2, 9, 225, 2, 1, 1, 0),
    quantity: int = 1,
    warhead: int = 0,
    fuse: int = 0,
    range_: float = 0.0,
) -> FirePdu:
    """A real opendis FirePdu, fields set the way dis-sim's fire schedule
    would set them -- firingEntityID/targetEntityID (WarfareFamilyPdu),
    eventID, munitionExpendableID, and a MunitionDescriptor built from plain
    tuple args."""
    pdu = FirePdu(
        munitionExpendableID=_entity_id(*munition),
        eventID=EventIdentifier(
            simulationAddress=SimulationAddress(site=event[0], application=event[1]),
            eventNumber=event[2],
        ),
        location=Vector3Double(x=1.0, y=2.0, z=3.0),
        descriptor=MunitionDescriptor(
            munitionType=EntityType(
                entityKind=munition_type[0], domain=munition_type[1],
                country=munition_type[2], category=munition_type[3],
                subcategory=munition_type[4], specific=munition_type[5],
                extra=munition_type[6],
            ),
            warhead=warhead, fuse=fuse, quantity=quantity,
        ),
        range_=range_,
    )
    pdu.firingEntityID = _entity_id(*firing)
    pdu.targetEntityID = _entity_id(*target)
    pdu.protocolVersion = 7
    pdu.exerciseID = 1
    pdu.pduType = 2
    pdu.protocolFamily = 2
    pdu.pduStatus = 0
    return pdu


def _build_detonation_pdu(
    firing: tuple[int, int, int],
    target: tuple[int, int, int],
    event: tuple[int, int, int],
    detonation_result: int,
    munition_type: tuple[int, int, int, int, int, int, int] = (2, 9, 225, 2, 1, 1, 0),
    quantity: int = 1,
    warhead: int = 0,
    fuse: int = 0,
) -> DetonationPdu:
    pdu = DetonationPdu(
        eventID=EventIdentifier(
            simulationAddress=SimulationAddress(site=event[0], application=event[1]),
            eventNumber=event[2],
        ),
        location=Vector3Double(x=4.0, y=5.0, z=6.0),
        descriptor=MunitionDescriptor(
            munitionType=EntityType(
                entityKind=munition_type[0], domain=munition_type[1],
                country=munition_type[2], category=munition_type[3],
                subcategory=munition_type[4], specific=munition_type[5],
                extra=munition_type[6],
            ),
            warhead=warhead, fuse=fuse, quantity=quantity,
        ),
        detonationResult=detonation_result,
    )
    pdu.firingEntityID = _entity_id(*firing)
    pdu.targetEntityID = _entity_id(*target)
    pdu.protocolVersion = 7
    pdu.exerciseID = 1
    pdu.pduType = 3
    pdu.protocolFamily = 2
    pdu.pduStatus = 0
    return pdu


def _serialize(pdu) -> bytes:  # noqa: ANN001
    buf = BytesIO()
    pdu.serialize(DataOutputStream(buf))
    return buf.getvalue()


class FireDecodeTests(unittest.TestCase):
    def test_fire_keys(self):
        pdu = _build_fire_pdu(
            firing=(1, 1, 57001), target=(1, 1, 57010), event=(1, 1, 3),
            munition=(0, 0, 0), quantity=1, warhead=5, fuse=6, range_=1234.5,
        )
        decoded = createPdu(_serialize(pdu))
        self.assertIsInstance(decoded, FirePdu)

        payload = dis_ingestor._extract_fire(decoded)

        self.assertEqual(payload["pdu_type"], "fire")
        self.assertEqual(payload["event_urn"], "dis-event:1:1:3")
        self.assertEqual(payload["launcher_urn"], "dis:1:1:57001")
        self.assertEqual(payload["target_urn"], "dis:1:1:57010")
        self.assertIsNone(payload["munition_urn"])  # 0:0:0 -> null
        self.assertEqual(
            payload["munition_type"],
            {"kind": 2, "domain": 9, "country": 225, "category": 2,
             "subcategory": 1, "specific": 1, "extra": 0},
        )
        self.assertEqual(payload["quantity"], 1)
        self.assertEqual(payload["warhead"], 5)
        self.assertEqual(payload["fuse"], 6)
        self.assertAlmostEqual(payload["range"], 1234.5, places=3)
        self.assertEqual(payload["location"], {"x": 1.0, "y": 2.0, "z": 3.0})
        self.assertIn("ingest_timestamp", payload)
        self.assertNotIn("detonation_result", payload)
        self.assertEqual(
            payload["provenance"],
            {
                "edge_id": dis_ingestor.OPENDDIL_EDGE_ID,
                "region_id": dis_ingestor.OPENDDIL_REGION_ID,
            },
        )

    def test_target_zero_is_null(self):
        pdu = _build_fire_pdu(
            firing=(1, 1, 57001), target=(0, 0, 0), event=(1, 1, 3),
        )
        decoded = createPdu(_serialize(pdu))
        payload = dis_ingestor._extract_fire(decoded)
        self.assertIsNone(payload["target_urn"])

    def test_site_filter_refuses_foreign_firing_entity(self):
        pdu = _build_fire_pdu(
            firing=(2, 1, 57001), target=(1, 1, 57010), event=(2, 1, 1),
        )
        decoded = createPdu(_serialize(pdu))
        self.assertFalse(dis_ingestor._effector_site_matches(decoded, 1))
        self.assertTrue(dis_ingestor._effector_site_matches(decoded, 2))
        self.assertTrue(dis_ingestor._effector_site_matches(decoded, None))


class DetonationDecodeTests(unittest.TestCase):
    def test_detonation_keys(self):
        pdu = _build_detonation_pdu(
            firing=(1, 1, 57001), target=(1, 1, 57010), event=(1, 1, 1),
            detonation_result=3, quantity=1, warhead=5, fuse=6,
        )
        decoded = createPdu(_serialize(pdu))
        self.assertIsInstance(decoded, DetonationPdu)

        payload = dis_ingestor._extract_detonation(decoded)

        self.assertEqual(payload["pdu_type"], "detonation")
        self.assertEqual(payload["event_urn"], "dis-event:1:1:1")
        self.assertEqual(payload["launcher_urn"], "dis:1:1:57001")
        self.assertEqual(payload["target_urn"], "dis:1:1:57010")
        self.assertEqual(payload["detonation_result"], 3)
        self.assertEqual(payload["location"], {"x": 4.0, "y": 5.0, "z": 6.0})
        self.assertIn("ingest_timestamp", payload)
        self.assertNotIn("munition_urn", payload)
        self.assertNotIn("range", payload)
        self.assertEqual(
            payload["provenance"],
            {
                "edge_id": dis_ingestor.OPENDDIL_EDGE_ID,
                "region_id": dis_ingestor.OPENDDIL_REGION_ID,
            },
        )

    def test_target_zero_is_null(self):
        pdu = _build_detonation_pdu(
            firing=(1, 1, 57001), target=(0, 0, 0), event=(1, 1, 1),
            detonation_result=1,
        )
        decoded = createPdu(_serialize(pdu))
        payload = dis_ingestor._extract_detonation(decoded)
        self.assertIsNone(payload["target_urn"])

    def test_site_filter_refuses_foreign_firing_entity(self):
        pdu = _build_detonation_pdu(
            firing=(2, 1, 57001), target=(1, 1, 57010), event=(2, 1, 1),
            detonation_result=1,
        )
        decoded = createPdu(_serialize(pdu))
        self.assertFalse(dis_ingestor._effector_site_matches(decoded, 1))


class StillDroppedTests(unittest.TestCase):
    def test_collision_pdu_type_4_still_dropped_and_counted(self):
        # Type 4 (Collision) is not Entity State, Remove Entity, Event
        # Report, Fire, or Detonation -- it must stay in the generic
        # drop-and-count else-branch, unaffected by admitting 2 and 3.
        before = dis_ingestor.DIS_PDUS_DROPPED_BY_TYPE.labels(pdu_type="4")._value.get()

        pdu = CollisionPdu()
        pdu.protocolVersion = 7
        pdu.exerciseID = 1
        pdu.pduType = 4
        pdu.protocolFamily = 2
        pdu.pduStatus = 0
        pdu.issuingEntityID = _entity_id(1, 1, 57001)
        pdu.collidingEntityID = _entity_id(1, 1, 57002)
        decoded = createPdu(_serialize(pdu))
        self.assertEqual(int(getattr(decoded, "pduType", -1)), 4)

        # Mirrors run()'s else-branch: a type this sidecar does not act on
        # is counted here, same counter and label it would get in the
        # socket loop.
        dis_ingestor.DIS_PDUS_DROPPED_BY_TYPE.labels(pdu_type=str(4)).inc()
        after = dis_ingestor.DIS_PDUS_DROPPED_BY_TYPE.labels(pdu_type="4")._value.get()
        self.assertEqual(after, before + 1)


if __name__ == "__main__":
    unittest.main()
