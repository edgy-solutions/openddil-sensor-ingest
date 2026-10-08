"""
Unit tests for the Resupply Received (PDU type 7, Logistics family) decode
path. Pure unit tests, no socket, no Kafka: they call dis_ingestor's
hand-written decoder directly against bytes built with opendis's own
ResupplyReceivedPdu (serialize side) or with struct.

The decoder is hand-written because opendis's parse for this PDU raises
whenever supplies are present; serialize is fine and is used to build input.

Run with: python -m unittest discover -s tests/effector
"""
from __future__ import annotations

import struct
import sys
import unittest
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from opendis.DataOutputStream import DataOutputStream  # noqa: E402
from opendis.dis7 import (  # noqa: E402
    EntityID,
    EntityType,
    ResupplyReceivedPdu,
    SupplyQuantity,
)

import dis_ingestor  # noqa: E402

TYPE_A = (2, 9, 225, 2, 1, 1, 0)
TYPE_B = (2, 9, 71, 3, 4, 5, 6)


def _build_opendis(receiver, supplier, supplies, timestamp=123456) -> bytes:
    pdu = ResupplyReceivedPdu(
        receivingEntityID=EntityID(*receiver),
        supplyingEntityID=EntityID(*supplier),
        supplies=[
            SupplyQuantity(
                supplyType=EntityType(
                    entityKind=t[0], domain=t[1], country=t[2], category=t[3],
                    subcategory=t[4], specific=t[5], extra=t[6]),
                quantity=q)
            for t, q in supplies
        ],
    )
    pdu.protocolVersion = 7
    pdu.exerciseID = 1
    pdu.pduType = 7
    pdu.protocolFamily = 3
    pdu.timestamp = timestamp
    pdu.pduStatus = 0
    # opendis leaves the header length at 0 on serialize; the real total
    # is known only after one pass, so stamp it and serialize again.
    buf = BytesIO()
    pdu.serialize(DataOutputStream(buf))
    pdu.length = len(buf.getvalue())
    buf = BytesIO()
    pdu.serialize(DataOutputStream(buf))
    return buf.getvalue()


def _build_struct(receiver, supplier, supplies, pad=3, timestamp=99) -> bytes:
    body = struct.pack(">HHHHHHB", *receiver, *supplier, len(supplies))
    body += b"\x00" * pad
    for t, q in supplies:
        body += struct.pack(">BBHBBBBf", *t, q)
    length = 12 + len(body)
    head = struct.pack(">BBBBIHBB", 7, 1, 7, 3, timestamp, length, 0, 0)
    return head + body


class ResupplyReceivedDecodeTests(unittest.TestCase):
    def test_opendis_built_bytes(self):
        data = _build_opendis((1, 2, 300), (1, 3, 400),
                              [(TYPE_A, 12.5), (TYPE_B, 3.0)])
        out = dis_ingestor._parse_resupply_received(data)
        self.assertEqual(out["pdu_type"], "resupply_received")
        self.assertEqual(out["event_urn"], "dis-resupply:1:2:300:123456")
        self.assertEqual(out["launcher_urn"], "dis:1:2:300")
        self.assertEqual(out["supplier_urn"], "dis:1:3:400")
        keys = ("kind", "domain", "country", "category",
                "subcategory", "specific", "extra")
        self.assertEqual(len(out["supplies"]), 2)
        self.assertEqual(out["supplies"][0]["munition_type"],
                         dict(zip(keys, TYPE_A)))
        self.assertEqual(out["supplies"][0]["quantity"], 12.5)
        self.assertEqual(out["supplies"][1]["munition_type"],
                         dict(zip(keys, TYPE_B)))
        self.assertEqual(out["supplies"][1]["quantity"], 3.0)
        self.assertIn("ingest_timestamp", out)
        self.assertEqual(set(out["provenance"]), {"edge_id", "region_id"})

    def test_pad3_and_pad4_parse_identically(self):
        sup = [(TYPE_A, 1.5), (TYPE_B, 2.0)]
        a = dis_ingestor._parse_resupply_received(
            _build_struct((1, 2, 3), (4, 5, 6), sup, pad=3))
        b = dis_ingestor._parse_resupply_received(
            _build_struct((1, 2, 3), (4, 5, 6), sup, pad=4))
        for d in (a, b):
            d.pop("ingest_timestamp")
        self.assertEqual(a, b)
        self.assertEqual(a["supplier_urn"], "dis:4:5:6")
        self.assertEqual(a["supplies"][1]["quantity"], 2.0)

    def test_bad_pad_raises(self):
        data = _build_struct((1, 2, 3), (4, 5, 6), [(TYPE_A, 1.0)], pad=5)
        with self.assertRaises(ValueError):
            dis_ingestor._parse_resupply_received(data)

    def test_truncated_raises(self):
        data = _build_struct((1, 2, 3), (4, 5, 6), [(TYPE_A, 1.0)])
        with self.assertRaises(ValueError):
            dis_ingestor._parse_resupply_received(data[:-1])

    def test_zero_supplies(self):
        for pad in (3, 4):
            out = dis_ingestor._parse_resupply_received(
                _build_struct((1, 2, 3), (4, 5, 6), [], pad=pad))
            self.assertEqual(out["supplies"], [])


class ResupplyReceivedFilterTests(unittest.TestCase):
    def _payload(self, receiver):
        return dis_ingestor._parse_resupply_received(
            _build_struct(receiver, (9, 9, 9), [(TYPE_A, 1.0)]))

    def test_other_site_dropped(self):
        p = self._payload((2, 1, 10))
        self.assertFalse(dis_ingestor._accept_resupply_received(p, 1, None))

    def test_same_site_accepted(self):
        p = self._payload((1, 1, 10))
        self.assertTrue(dis_ingestor._accept_resupply_received(p, 1, None))
        self.assertTrue(dis_ingestor._accept_resupply_received(p, None, None))

    def test_not_admitted_dropped_and_admitted_accepted(self):
        p = self._payload((1, 1, 10))
        self.assertFalse(dis_ingestor._accept_resupply_received(
            p, 1, frozenset({"dis:1:1:11"})))
        self.assertTrue(dis_ingestor._accept_resupply_received(
            p, 1, frozenset({"dis:1:1:10"})))

    def test_supplier_is_not_the_filter_key(self):
        p = self._payload((1, 1, 10))
        self.assertFalse(dis_ingestor._accept_resupply_received(
            p, 1, frozenset({"dis:9:9:9"})))


if __name__ == "__main__":
    unittest.main()
