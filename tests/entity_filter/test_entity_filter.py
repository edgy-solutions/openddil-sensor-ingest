"""
Unit tests for the DIS_ADMITTED_ENTITY_IDS entity filter.

PURE UNIT TESTS, NO INFRASTRUCTURE, same contract as
tests/multicast_site_filter (same floor/ceiling/negative-control shape, at
the predicate level instead of a docker-compose stack) and as
tests/readiness/test_readiness.py (same reasoning for calling the accept
path directly rather than racing a real socket).

WHY A SECOND FILTER ON TOP OF THE SITE FILTER. One DIS site can span two
edges (a shared multicast group, or a unicast fan-out, reaching both edges'
sidecars), so the site filter alone can no longer tell the two sidecars'
entities apart -- both admit the whole site. DIS_ADMITTED_ENTITY_IDS is the
declared list that splits it, applied strictly after the site filter and
keyed per PDU type on exactly the same entity field the site filter already
uses for that type.

Run with: python -m unittest discover -s tests/entity_filter
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
    EntityID,
    EntityStatePdu,
    EntityType,
    EventIdentifier,
    EventReportPdu,
    FirePdu,
    MunitionDescriptor,
    RemoveEntityPdu,
    SimulationAddress,
    Vector3Double,
)

import dis_ingestor  # noqa: E402
import ready  # noqa: E402


def _entity_id(site: int, application: int, entity: int) -> EntityID:
    return EntityID(siteID=site, applicationID=application, entityID=entity)


def _serialize_roundtrip(pdu) -> object:  # noqa: ANN001
    """Serialize and re-decode, so a test exercises the real wire path
    (createPdu()) rather than a hand-built object the decoder never saw."""
    buf = BytesIO()
    pdu.serialize(DataOutputStream(buf))
    return createPdu(buf.getvalue())


def _entity_state_pdu(site: int, application: int, entity: int) -> EntityStatePdu:
    pdu = EntityStatePdu()
    pdu.protocolVersion = 7
    pdu.exerciseID = 1
    pdu.pduType = 1
    pdu.protocolFamily = 1
    pdu.pduStatus = 0
    pdu.entityAppearance = 0
    pdu.capabilities = 0
    pdu.entityID.siteID = site
    pdu.entityID.applicationID = application
    pdu.entityID.entityID = entity
    return _serialize_roundtrip(pdu)


def _remove_entity_pdu(
    originating: tuple[int, int, int],
    receiving: tuple[int, int, int],
    request_id: int = 1,
) -> RemoveEntityPdu:
    pdu = RemoveEntityPdu()
    pdu.protocolVersion = 7
    pdu.exerciseID = 1
    pdu.pduType = 12
    pdu.protocolFamily = 5
    pdu.pduStatus = 0
    pdu.originatingEntityID.siteID = originating[0]
    pdu.originatingEntityID.applicationID = originating[1]
    pdu.originatingEntityID.entityID = originating[2]
    pdu.receivingEntityID.siteID = receiving[0]
    pdu.receivingEntityID.applicationID = receiving[1]
    pdu.receivingEntityID.entityID = receiving[2]
    pdu.requestID = request_id
    return _serialize_roundtrip(pdu)


def _event_report_pdu(
    originating: tuple[int, int, int],
    receiving: tuple[int, int, int],
    event_type: int = 1,
) -> EventReportPdu:
    pdu = EventReportPdu(eventType=event_type)
    pdu.protocolVersion = 7
    pdu.exerciseID = 1
    pdu.pduType = 21
    pdu.protocolFamily = 5
    pdu.pduStatus = 0
    pdu.originatingEntityID.siteID = originating[0]
    pdu.originatingEntityID.applicationID = originating[1]
    pdu.originatingEntityID.entityID = originating[2]
    pdu.receivingEntityID.siteID = receiving[0]
    pdu.receivingEntityID.applicationID = receiving[1]
    pdu.receivingEntityID.entityID = receiving[2]
    return _serialize_roundtrip(pdu)


def _fire_pdu(
    firing: tuple[int, int, int],
    target: tuple[int, int, int],
) -> FirePdu:
    pdu = FirePdu(
        munitionExpendableID=_entity_id(0, 0, 0),
        eventID=EventIdentifier(
            simulationAddress=SimulationAddress(site=firing[0], application=firing[1]),
            eventNumber=1,
        ),
        location=Vector3Double(x=1.0, y=2.0, z=3.0),
        descriptor=MunitionDescriptor(
            munitionType=EntityType(
                entityKind=2, domain=9, country=225, category=2,
                subcategory=1, specific=1, extra=0,
            ),
            warhead=0, fuse=0, quantity=1,
        ),
        range_=0.0,
    )
    pdu.firingEntityID = _entity_id(*firing)
    pdu.targetEntityID = _entity_id(*target)
    pdu.protocolVersion = 7
    pdu.exerciseID = 1
    pdu.pduType = 2
    pdu.protocolFamily = 2
    pdu.pduStatus = 0
    return _serialize_roundtrip(pdu)


class ParseAdmittedEntityIdsTests(unittest.TestCase):
    def test_unset_is_none(self):
        self.assertIsNone(dis_ingestor._parse_admitted_entity_ids(""))

    def test_whitespace_only_is_none(self):
        self.assertIsNone(dis_ingestor._parse_admitted_entity_ids("   "))

    def test_comma_separated(self):
        result = dis_ingestor._parse_admitted_entity_ids("dis:1:1:100,dis:1:1:200")
        self.assertEqual(result, frozenset({"dis:1:1:100", "dis:1:1:200"}))

    def test_whitespace_separated(self):
        result = dis_ingestor._parse_admitted_entity_ids("dis:1:1:100  dis:1:1:200\ndis:1:1:300")
        self.assertEqual(result, frozenset({"dis:1:1:100", "dis:1:1:200", "dis:1:1:300"}))

    def test_mixed_comma_and_whitespace(self):
        result = dis_ingestor._parse_admitted_entity_ids(" dis:1:1:100, dis:1:1:200 ,dis:1:1:300")
        self.assertEqual(result, frozenset({"dis:1:1:100", "dis:1:1:200", "dis:1:1:300"}))

    def test_malformed_id_exits_nonzero(self):
        with self.assertRaises(SystemExit) as ctx:
            dis_ingestor._parse_admitted_entity_ids("dis:1:1:100,not-a-urn")
        self.assertNotEqual(ctx.exception.code, 0)

    def test_duplicate_id_exits_nonzero(self):
        with self.assertRaises(SystemExit) as ctx:
            dis_ingestor._parse_admitted_entity_ids("dis:1:1:100,dis:1:1:100")
        self.assertNotEqual(ctx.exception.code, 0)


class EntityAdmittedPredicateTests(unittest.TestCase):
    def test_none_admitted_accepts_everything(self):
        eid = _entity_id(1, 1, 999)
        self.assertTrue(dis_ingestor._entity_admitted(eid, None))

    def test_listed_id_admitted(self):
        admitted = frozenset({"dis:1:1:100"})
        self.assertTrue(dis_ingestor._entity_admitted(_entity_id(1, 1, 100), admitted))

    def test_unlisted_id_in_same_site_rejected(self):
        admitted = frozenset({"dis:1:1:100"})
        self.assertFalse(dis_ingestor._entity_admitted(_entity_id(1, 1, 200), admitted))


class AcceptEntityStateEntityFilterTests(unittest.TestCase):
    def test_admitted_entity_accepted_and_advances_readiness(self):
        admitted = frozenset({"dis:1:1:100"})
        pdu = _entity_state_pdu(1, 1, 100)

        before = ready.state()["entity_pdus"]
        accepted = dis_ingestor._accept_entity_state(pdu, 1, admitted)
        after = ready.state()["entity_pdus"]

        self.assertTrue(accepted)
        self.assertEqual(after, before + 1)

    def test_other_entity_same_site_dropped_reason_entity_no_readiness(self):
        admitted = frozenset({"dis:1:1:100"})
        pdu = _entity_state_pdu(1, 1, 200)  # same site, not on the list

        before_filtered = dis_ingestor.DIS_PDUS_FILTERED.labels(reason="entity")._value.get()
        before_ready = ready.state()["entity_pdus"]

        accepted = dis_ingestor._accept_entity_state(pdu, 1, admitted)

        self.assertFalse(accepted)
        self.assertEqual(
            dis_ingestor.DIS_PDUS_FILTERED.labels(reason="entity")._value.get(),
            before_filtered + 1,
        )
        self.assertEqual(ready.state()["entity_pdus"], before_ready)

    def test_unset_admitted_accepts_any_entity_in_site(self):
        pdu = _entity_state_pdu(1, 1, 12345)
        self.assertTrue(dis_ingestor._accept_entity_state(pdu, 1, None))


def _filtered_entity_count() -> float:
    return dis_ingestor.DIS_PDUS_FILTERED.labels(reason="entity")._value.get()


class FireDetonationEntityFilterKeyingTests(unittest.TestCase):
    """A Fire PDU whose TARGET is admitted but whose firing entity is not
    must still be dropped -- _accept_effector (the function run() actually
    calls) keys its entity filter on firingEntityID (the launcher), the
    same field its site filter already uses, never on targetEntityID.

    These tests call _accept_effector itself, not _entity_admitted in
    isolation: calling the predicate directly on a hand-picked field is
    tautological -- it would still pass if _accept_effector's wiring
    actually consulted targetEntityID. Going through _accept_effector and
    asserting the reason="entity" counter delta is what makes the wrong
    wiring (see the guard-flip test below) observably fail here.
    """

    def test_target_admitted_firing_not_still_dropped_reason_entity(self):
        admitted = frozenset({"dis:1:1:999"})  # the TARGET's urn, not firing's
        pdu = _fire_pdu(firing=(1, 1, 500), target=(1, 1, 999))

        before = _filtered_entity_count()
        accepted = dis_ingestor._accept_effector(pdu, 1, admitted)
        after = _filtered_entity_count()

        self.assertFalse(accepted)
        self.assertEqual(after, before + 1)

    def test_admitted_firing_entity_passes(self):
        admitted = frozenset({"dis:1:1:500"})
        pdu = _fire_pdu(firing=(1, 1, 500), target=(1, 1, 999))

        before = _filtered_entity_count()
        accepted = dis_ingestor._accept_effector(pdu, 1, admitted)
        after = _filtered_entity_count()

        self.assertTrue(accepted)
        self.assertEqual(after, before)


class RemoveEntityFilterKeyingTests(unittest.TestCase):
    """_accept_remove_entity (the function run() actually calls) keys both
    its site and entity filter on receivingEntityID (the entity being
    removed), never originatingEntityID. See FireDetonationEntityFilter
    KeyingTests above for why this calls _accept_remove_entity rather than
    _entity_admitted directly."""

    def test_originating_admitted_receiving_not_still_dropped_reason_entity(self):
        admitted = frozenset({"dis:1:1:999"})  # originating's urn, not receiving's
        pdu = _remove_entity_pdu(originating=(1, 1, 999), receiving=(1, 1, 500))

        before = _filtered_entity_count()
        accepted = dis_ingestor._accept_remove_entity(pdu, 1, admitted)
        after = _filtered_entity_count()

        self.assertFalse(accepted)
        self.assertEqual(after, before + 1)

    def test_admitted_receiving_entity_passes(self):
        admitted = frozenset({"dis:1:1:500"})
        pdu = _remove_entity_pdu(originating=(1, 1, 999), receiving=(1, 1, 500))

        before = _filtered_entity_count()
        accepted = dis_ingestor._accept_remove_entity(pdu, 1, admitted)
        after = _filtered_entity_count()

        self.assertTrue(accepted)
        self.assertEqual(after, before)


class EventReportFilterKeyingTests(unittest.TestCase):
    """_accept_event_report (the function run() actually calls) keys both
    its site and entity filter on originatingEntityID, never
    receivingEntityID. See FireDetonationEntityFilterKeyingTests above for
    why this calls _accept_event_report rather than _entity_admitted
    directly."""

    def test_receiving_admitted_originating_not_still_dropped_reason_entity(self):
        admitted = frozenset({"dis:1:1:999"})  # receiving's urn, not originating's
        pdu = _event_report_pdu(originating=(1, 1, 500), receiving=(1, 1, 999))

        before = _filtered_entity_count()
        accepted = dis_ingestor._accept_event_report(pdu, 1, admitted)
        after = _filtered_entity_count()

        self.assertFalse(accepted)
        self.assertEqual(after, before + 1)

    def test_admitted_originating_entity_passes(self):
        admitted = frozenset({"dis:1:1:500"})
        pdu = _event_report_pdu(originating=(1, 1, 500), receiving=(1, 1, 999))

        before = _filtered_entity_count()
        accepted = dis_ingestor._accept_event_report(pdu, 1, admitted)
        after = _filtered_entity_count()

        self.assertTrue(accepted)
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
