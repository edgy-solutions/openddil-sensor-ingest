"""
Unit tests for ready.py's readiness math, plus the dis_ingestor accept path
that feeds it.

PURE UNIT TESTS, NO INFRASTRUCTURE, NO SLEEPING. readiness() is a pure
function of (now, started, last_entity, count, window) -- these tests call
it directly with hand-picked numbers rather than racing real monotonic
time, the same reasoning tests/remove_entity/test_remove_entity_decode.py
gives for calling dis_ingestor's decode helpers directly against fabricated
bytes instead of a real socket.

Run with: python -m unittest discover -s tests/readiness
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
from opendis.dis7 import EntityStatePdu  # noqa: E402

import dis_ingestor  # noqa: E402
import ready  # noqa: E402


# ---------------------------------------------------------------------------
# ready.readiness() -- pure function, hand-picked numbers
# ---------------------------------------------------------------------------
class ReadinessMathTests(unittest.TestCase):
    def test_within_allowance_with_no_entity_ever_is_ready(self):
        # now - started = 10, window = 90: still inside the pre-run grace,
        # and last_entity=0.0 (never) is the "simulator hasn't run yet" case
        # this window exists for.
        result = ready.readiness(now=10.0, started=0.0, last_entity=0.0, count=0, window=90.0)
        self.assertTrue(result["ready"])

    def test_past_allowance_with_no_entity_ever_is_not_ready(self):
        result = ready.readiness(now=200.0, started=0.0, last_entity=0.0, count=0, window=90.0)
        self.assertFalse(result["ready"])

    def test_past_allowance_with_recent_entity_is_ready(self):
        # Uptime is 200s (past the 90s window), but an entity PDU arrived
        # 5s ago -- age <= window keeps it ready regardless of uptime.
        result = ready.readiness(now=200.0, started=0.0, last_entity=195.0, count=3, window=90.0)
        self.assertTrue(result["ready"])

    def test_past_allowance_with_stale_entity_is_not_ready(self):
        # last_entity was 100s ago (> window) AND uptime is past the
        # allowance too -- the simulator was running, then stopped talking,
        # and this is the case readiness must now catch.
        result = ready.readiness(now=200.0, started=0.0, last_entity=100.0, count=3, window=90.0)
        self.assertFalse(result["ready"])

    def test_result_has_exactly_the_expected_keys(self):
        result = ready.readiness(now=10.0, started=0.0, last_entity=0.0, count=0, window=90.0)
        self.assertEqual(
            set(result.keys()),
            {"ready", "entity_pdus", "last_entity_age_s", "uptime_s", "window_s"},
        )


# ---------------------------------------------------------------------------
# dis_ingestor._accept_entity_state() wires ready's module state
# ---------------------------------------------------------------------------
def _entity_state_pdu(site: int) -> EntityStatePdu:
    """Minimal round-tripped EntityStatePdu for one site.

    Same construct-serialize-decode approach as
    tests/multicast_site_filter/send_pdus.py's build() and
    test_remove_entity_decode.py's test_espdu_record_shape_unchanged: build
    the real opendis object, serialize it, decode it with createPdu(), so
    _accept_entity_state() sees exactly what the wire would hand it.
    """
    pdu = EntityStatePdu()
    pdu.protocolVersion = 7
    pdu.exerciseID = 1
    pdu.pduType = 1
    pdu.protocolFamily = 1
    pdu.pduStatus = 0
    pdu.entityAppearance = 0
    pdu.capabilities = 0
    pdu.entityID.siteID = site
    pdu.entityID.applicationID = 1
    pdu.entityID.entityID = 2000

    buf = BytesIO()
    pdu.serialize(DataOutputStream(buf))
    return createPdu(buf.getvalue())


class AcceptEntityStateReadinessTests(unittest.TestCase):
    # ready's module state is global across the whole pytest run, so these
    # assert on DELTAS rather than absolute values -- the same reason
    # test_remove_entity_decode.py's short-pdu test doesn't assert on
    # exact counters shared with other tests.

    def test_same_site_accept_advances_ready_state(self):
        configured_site = 42
        pdu = _entity_state_pdu(site=configured_site)

        before = ready.state()["entity_pdus"]
        accepted = dis_ingestor._accept_entity_state(pdu, configured_site)
        after = ready.state()

        self.assertTrue(accepted)
        self.assertEqual(after["entity_pdus"], before + 1)
        self.assertIsNotNone(after["last_entity_age_s"])
        self.assertLess(after["last_entity_age_s"], 5.0)

    def test_other_site_accept_does_not_advance_ready_state(self):
        configured_site = 42
        pdu = _entity_state_pdu(site=configured_site)

        # First, a same-site PDU to establish a baseline count.
        dis_ingestor._accept_entity_state(pdu, configured_site)
        baseline = ready.state()["entity_pdus"]

        other_site_pdu = _entity_state_pdu(site=configured_site + 1)
        accepted = dis_ingestor._accept_entity_state(other_site_pdu, configured_site)
        after = ready.state()["entity_pdus"]

        self.assertFalse(accepted)
        self.assertEqual(after, baseline)


if __name__ == "__main__":
    unittest.main()
