"""
Unit tests for the Event Report (PDU type 21) decode path.

PURE UNIT TESTS, NO INFRASTRUCTURE, same contract as
tests/remove_entity/test_remove_entity_decode.py: these call dis_ingestor's
decode helpers directly against bytes built with opendis's own
EventReportPdu/FixedDatum/VariableDatum classes and check what comes back.
No socket, no Kafka, no compose stack.

PURE TRANSPORT, NOT INTERPRETATION. These tests pin what arrived, not what
it means — eventType and the datum ids are opaque integers throughout.

Run with: python -m unittest discover -s tests/event_report
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
from opendis.dis7 import EventReportPdu, FixedDatum, VariableDatum  # noqa: E402

import dis_ingestor  # noqa: E402


def _build_event_report_pdu(
    originating: tuple[int, int, int],
    receiving: tuple[int, int, int],
    event_type: int,
    fixed_datums: dict[int, int] | None = None,
    variable_datums: dict[int, str] | None = None,
) -> EventReportPdu:
    """A real opendis EventReportPdu, fields set the way dis-sim's
    event_report_pdu() sets them — originating/receiving EntityID, eventType,
    and fixed/variable datum records built from plain dict args."""
    fixed = [FixedDatum(did, val) for did, val in (fixed_datums or {}).items()]
    variable = []
    for did, text in (variable_datums or {}).items():
        data = list(text.encode("utf-8"))
        variable.append(VariableDatum(did, len(data) * 8, data))

    pdu = EventReportPdu(eventType=event_type, fixedDatumRecords=fixed,
                         variableDatumRecords=variable)
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
    return pdu


def _serialize(pdu) -> bytes:  # noqa: ANN001
    buf = BytesIO()
    pdu.serialize(DataOutputStream(buf))
    return buf.getvalue()


class EventReportDecodeTests(unittest.TestCase):
    def test_valid_event_report_decodes_and_extracts(self):
        pdu = _build_event_report_pdu(
            originating=(1, 1, 1005),
            receiving=(0, 0, 0),
            event_type=42,
            fixed_datums={7: 123},
            # "abc" (3 bytes) pins the non-multiple-of-8 padding round trip.
            variable_datums={11: "abc", 22: "12345678"},
        )
        data = _serialize(pdu)
        decoded = createPdu(data)

        self.assertIsInstance(decoded, EventReportPdu)
        self.assertTrue(dis_ingestor._event_report_site_matches(decoded, None))
        self.assertTrue(dis_ingestor._event_report_site_matches(decoded, 1))
        self.assertFalse(dis_ingestor._event_report_site_matches(decoded, 2))

        payload = dis_ingestor._extract_event_report(decoded)

        self.assertEqual(payload["pdu_type"], "event_report")
        self.assertEqual(
            payload["dis_entity_id"],
            {"site": 1, "application": 1, "entity": 1005},
        )
        self.assertEqual(payload["entity_id_urn"], "dis:1:1:1005")
        self.assertEqual(
            payload["receiving_entity_id"],
            {"site": 0, "application": 0, "entity": 0},
        )
        self.assertEqual(payload["event_type"], 42)
        self.assertEqual(payload["fixed_datums"], {"7": 123})
        self.assertEqual(payload["variable_datums"], {"11": "abc", "22": "12345678"})
        self.assertIn("ingest_timestamp", payload)
        # Not the ESPDU or Remove Entity record shape.
        self.assertNotIn("appearance", payload)
        self.assertNotIn("location_ecef", payload)
        self.assertNotIn("request_id", payload)

    def test_site_filtered_pdu_is_not_a_match(self):
        pdu = _build_event_report_pdu(
            originating=(2, 1, 1005),
            receiving=(0, 0, 0),
            event_type=1,
        )
        data = _serialize(pdu)
        decoded = createPdu(data)
        # This sidecar is configured for site 1; the PDU originates at site
        # 2 — run()'s loop would count it dis_pdus_filtered_total{reason=
        # "site"} and `continue` before ever calling _extract_event_report,
        # i.e. not produced.
        self.assertFalse(dis_ingestor._event_report_site_matches(decoded, 1))

    def test_short_pdu_raises_rather_than_returning_garbage(self):
        # Truncated Event Report PDU. The production path (dis_ingestor.
        # run()'s main loop) wraps createPdu() in a try/except that counts
        # this under DIS_DECODE_ERRORS and drops it (`continue` before the
        # Kafka produce call) — see the module's PDU-decode block. This
        # test pins the decoder-level behaviour that path relies on: a
        # short PDU raises rather than silently returning a
        # partially-populated object. Same shape as remove_entity's
        # test_short_pdu_raises_rather_than_returning_garbage.
        pdu = _build_event_report_pdu(
            originating=(1, 1, 1005),
            receiving=(0, 0, 0),
            event_type=42,
            variable_datums={11: "abc"},
        )
        data = _serialize(pdu)
        truncated = data[:20]  # cuts off mid-way through the datum records
        with self.assertRaises(Exception):
            createPdu(truncated)

    def test_variable_datum_string_decode_errors_replace_and_strips_nul(self):
        # Invalid UTF-8 byte (0xFF) decodes with errors="replace" rather
        # than raising, and a trailing NUL (defensively stripped, same
        # contract as the ESPDU marking field) does not leak into the
        # string.
        pdu = EventReportPdu(
            eventType=1,
            variableDatumRecords=[VariableDatum(5, 32, [0x41, 0xFF, 0x00, 0x00])],
        )
        pdu.protocolVersion = 7
        pdu.exerciseID = 1
        pdu.pduType = 21
        pdu.protocolFamily = 5
        pdu.pduStatus = 0
        data = _serialize(pdu)
        decoded = createPdu(data)

        payload = dis_ingestor._extract_event_report(decoded)
        self.assertEqual(payload["variable_datums"]["5"], "A�")


if __name__ == "__main__":
    unittest.main()
