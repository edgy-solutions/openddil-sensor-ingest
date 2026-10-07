"""
Unit tests for appearance.decode()'s launcher_raised bit (ADR-0044
amendment, "posture, a third column") and for the two refusals the module
docstring calls out, confirmed to still hold once that bit is added.

PURE UNIT TESTS, NO INFRASTRUCTURE, NO DISK I/O: appearance._load() reads
dis_appearance.yaml from ONTOLOGY_DIR and caches it in the module-level
_TABLE global, so these tests set _TABLE directly to a small hand-written
fixture (same shape as the real ontology's "1_1"/"1_2" blocks) instead of
writing a YAML file to disk -- same reasoning tests/readiness/test_
readiness.py gives for calling pure functions with hand-picked inputs
rather than exercising real I/O. _TABLE is reset in setUp/tearDown so one
test's fixture never leaks into the next.

Run with: python -m unittest discover -s tests/appearance
(or via pytest, if available -- no pytest-only features are used).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import appearance  # noqa: E402

_SITE = 1

# Mirrors the real ontology's declared source + "1_1" (Land, HAS launcher)
# / "1_2" (Air, has NO launcher) blocks, trimmed to the bits these tests
# exercise.
_FIXTURE_TABLE = {
    "populating_sources": {"1": {}},
    "appearance": {
        "1_1": {
            "damage": {"bits": [3, 4], "values": {0: "NONE", 1: "SLIGHT", 2: "MODERATE", 3: "DESTROYED"}},
            "mobility_kill": {"bit": 1},
            "firepower_kill": {"bit": 2},
            "power_plant": {"bit": 21},
            "deactivated": {"bit": 22},
            "launcher": {"bit": 15},
        },
        "1_2": {
            "damage": {"bits": [3, 4], "values": {0: "NONE", 1: "SLIGHT", 2: "MODERATE", 3: "DESTROYED"}},
            "mobility_kill": {"bit": 1},
            "power_plant": {"bit": 21},
            "deactivated": {"bit": 22},
            # No "launcher" entry -- air has no such bit, same reason it has
            # no firepower_kill entry.
        },
    },
}

_POWERPLANT = 1 << 21
_LAUNCHER_RAISED = 1 << 15


class LauncherRaisedDecodeTests(unittest.TestCase):
    def setUp(self):
        appearance._TABLE = dict(_FIXTURE_TABLE)

    def tearDown(self):
        appearance._TABLE = None

    def test_launcher_raised_true_when_bit_set_on_land(self):
        bits = _POWERPLANT | _LAUNCHER_RAISED
        out = appearance.decode(bits, kind=1, domain=1, site_id=_SITE)
        self.assertEqual(out["launcher_raised"], True)

    def test_launcher_raised_false_is_an_explicit_claim_not_absence(self):
        # Power plant on (a genuine claim), launcher bit clear: the key is
        # PRESENT with value False, not missing -- the explicit "stowed"
        # claim the posture state machine's cold-start row relies on.
        bits = _POWERPLANT
        out = appearance.decode(bits, kind=1, domain=1, site_id=_SITE)
        self.assertIn("launcher_raised", out)
        self.assertEqual(out["launcher_raised"], False)

    def test_air_domain_has_no_launcher_key_even_if_the_bit_is_set(self):
        # "A domain without a field does not get one" (module docstring,
        # refusal 2) -- bit 15 happens to be set here, but 1_2's block has
        # no "launcher" entry, so no claim is manufactured from it.
        bits = _POWERPLANT | _LAUNCHER_RAISED
        out = appearance.decode(bits, kind=1, domain=2, site_id=_SITE)
        self.assertNotIn("launcher_raised", out)

    def test_undeclared_source_decodes_nothing_launcher_included(self):
        # "An undeclared source is not decoded" (refusal 1) -- site 2 is not
        # in populating_sources, so even a nonzero field with the launcher
        # bit set yields {}, not a manufactured claim.
        bits = _POWERPLANT | _LAUNCHER_RAISED
        out = appearance.decode(bits, kind=1, domain=1, site_id=2)
        self.assertEqual(out, {})

    def test_zero_guard_withholds_launcher_raised_too(self):
        # Exactly zero means "nothing was said" -- the zero guard fires
        # before any field (including launcher) is read.
        out = appearance.decode(0, kind=1, domain=1, site_id=_SITE)
        self.assertEqual(out, {})

    def test_other_axes_unaffected_by_the_new_bit(self):
        # Guards that adding launcher decoding did not disturb the
        # pre-existing axes' bit math.
        bits = _POWERPLANT | _LAUNCHER_RAISED | (2 << 3)  # damage=MODERATE
        out = appearance.decode(bits, kind=1, domain=1, site_id=_SITE)
        self.assertEqual(out["damage"], "MODERATE")
        self.assertEqual(out["power_plant_on"], True)
        self.assertEqual(out["mobility_kill"], False)
        self.assertEqual(out["firepower_kill"], False)
        self.assertEqual(out["deactivated"], False)
        self.assertEqual(out["launcher_raised"], True)


if __name__ == "__main__":
    unittest.main()
