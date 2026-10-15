"""
Unit tests for munition claims (ADR-0049 section 2).

PURE UNIT TESTS, NO INFRASTRUCTURE, same shape as tests/entity_filter: the
accept helpers are called directly rather than racing a real socket.

A munition's id is minted at launch, so it is never on the admit list; an
accepted Fire claims it, and Entity State for a claimed id is then admitted.
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
    EntityID,
    EntityStatePdu,
    EntityType,
    EventIdentifier,
    FirePdu,
    MunitionDescriptor,
    SimulationAddress,
    Vector3Double,
)

import dis_ingestor  # noqa: E402
import ready  # noqa: E402

ADMITTED = frozenset({"dis:1:1:1"})
MUNITION = "dis:1:1:5001"


def _roundtrip(pdu):  # noqa: ANN001
    buf = BytesIO()
    pdu.serialize(DataOutputStream(buf))
    return createPdu(buf.getvalue())


def _espdu(entity: int, kind: int):
    pdu = EntityStatePdu()
    pdu.protocolVersion = 7
    pdu.exerciseID = 1
    pdu.pduType = 1
    pdu.protocolFamily = 1
    pdu.pduStatus = 0
    pdu.entityAppearance = 0
    pdu.capabilities = 0
    pdu.entityID.siteID = 1
    pdu.entityID.applicationID = 1
    pdu.entityID.entityID = entity
    pdu.entityType.entityKind = kind
    return _roundtrip(pdu)


def _fire(launcher: int, munition: int):
    pdu = FirePdu(
        munitionExpendableID=EntityID(siteID=1, applicationID=1, entityID=munition),
        eventID=EventIdentifier(
            simulationAddress=SimulationAddress(site=1, application=1),
            eventNumber=1,
        ),
        location=Vector3Double(x=1.0, y=2.0, z=3.0),
        descriptor=MunitionDescriptor(
            munitionType=EntityType(entityKind=2, domain=9, country=225,
                                    category=2, subcategory=1, specific=1,
                                    extra=0),
            warhead=0, fuse=0, quantity=1,
        ),
        range_=0.0,
    )
    pdu.firingEntityID = EntityID(siteID=1, applicationID=1, entityID=launcher)
    pdu.targetEntityID = EntityID(siteID=0, applicationID=0, entityID=0)
    pdu.protocolVersion = 7
    pdu.exerciseID = 1
    pdu.pduType = 2
    pdu.protocolFamily = 2
    pdu.pduStatus = 0
    return _roundtrip(pdu)


def _filtered(reason: str) -> float:
    return dis_ingestor.DIS_PDUS_FILTERED.labels(reason=reason)._value.get()


def _fire_through_accept(launcher: int, munition: int, now: float) -> bool:
    """The same accept-then-claim sequence the run loop performs."""
    pdu = _fire(launcher, munition)
    if not dis_ingestor._accept_effector(pdu, 1, ADMITTED):
        return False
    dis_ingestor._note_fire_claim(dis_ingestor._extract_fire(pdu), now)
    return True


class MunitionClaimTests(unittest.TestCase):
    def setUp(self):
        dis_ingestor._munition_claims.clear()

    def test_unclaimed_munition_refused_with_reason(self):
        before = _filtered("munition_unclaimed")
        self.assertFalse(dis_ingestor._accept_entity_state(_espdu(5001, 2), 1, ADMITTED))
        self.assertEqual(_filtered("munition_unclaimed") - before, 1)

    def test_unadmitted_platform_still_reason_entity(self):
        before = _filtered("entity")
        self.assertFalse(dis_ingestor._accept_entity_state(_espdu(77, 1), 1, ADMITTED))
        self.assertEqual(_filtered("entity") - before, 1)

    def test_claimed_after_accepted_fire(self):
        now = dis_ingestor.time.monotonic()
        self.assertTrue(_fire_through_accept(1, 5001, now))
        self.assertTrue(dis_ingestor._accept_entity_state(_espdu(5001, 2), 1, ADMITTED))

    def test_fire_from_unadmitted_launcher_does_not_claim(self):
        now = dis_ingestor.time.monotonic()
        self.assertFalse(_fire_through_accept(99, 5001, now))
        self.assertFalse(dis_ingestor._munition_claimed(MUNITION, now))
        self.assertFalse(dis_ingestor._accept_entity_state(_espdu(5001, 2), 1, ADMITTED))

    def test_claim_expires_after_ttl(self):
        dis_ingestor._claim_munition(MUNITION, 1000.0)
        ttl = dis_ingestor.DIS_MUNITION_CLAIM_TTL_S
        self.assertTrue(dis_ingestor._munition_claimed(MUNITION, 1000.0 + ttl - 1))
        self.assertFalse(dis_ingestor._munition_claimed(MUNITION, 1000.0 + ttl + 1))
        self.assertNotIn(MUNITION, dis_ingestor._munition_claims)

    def test_claimed_admit_does_not_mark_readiness(self):
        dis_ingestor._claim_munition(MUNITION, dis_ingestor.time.monotonic())
        count = ready.state()["entity_pdus"]
        self.assertTrue(dis_ingestor._accept_entity_state(_espdu(5001, 2), 1, ADMITTED))
        self.assertEqual(ready.state()["entity_pdus"], count)

    def test_no_filter_admits_everything(self):
        self.assertTrue(dis_ingestor._accept_entity_state(_espdu(5001, 2), 1, None))
        self.assertTrue(dis_ingestor._accept_entity_state(_espdu(77, 1), 1, None))

    def test_bad_ttl_refuses_to_start(self):
        for raw in ("0", "-5", "abc", "nan"):
            with self.assertRaises(SystemExit):
                dis_ingestor._parse_munition_claim_ttl(raw)


if __name__ == "__main__":
    unittest.main()
