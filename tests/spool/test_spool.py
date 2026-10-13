"""Spool: durable one-shot record store. Run: python -m unittest discover -s tests/spool"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import spool  # noqa: E402


class SpoolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name
        self.t = [1_000_000.0]
        self.clock = lambda: self.t[0]

    def test_round_trip(self):
        s = spool.Spool(self.dir, 10, clock=self.clock)
        name = s.write("topic-a", "k1", '{"a":1}')
        self.assertEqual(s.pending(), [name])
        self.assertEqual(s.read(name), {"topic": "topic-a", "key": "k1", "value": '{"a":1}'})
        s.ack(name)
        self.assertEqual(s.pending(), [])

    def test_filename_order_is_age_order(self):
        s = spool.Spool(self.dir, 10, clock=self.clock)
        names = []
        for i in range(5):
            names.append(s.write("t", "k", str(i)))
            self.t[0] += 0.5
        # same millisecond too: seq breaks the tie
        names.append(s.write("t", "k", "x"))
        names.append(s.write("t", "k", "y"))
        self.assertEqual(s.pending(), names)

    def test_max_entries_drops_oldest_and_counts(self):
        s = spool.Spool(self.dir, 3, clock=self.clock)
        before = spool.DIS_SPOOL_DROPPED._value.get()
        names = [s.write("t", "k", str(i)) for i in range(5)]
        self.assertEqual(s.pending(), names[2:])
        self.assertEqual(s.dropped, 2)
        self.assertEqual(spool.DIS_SPOOL_DROPPED._value.get() - before, 2)

    def test_tmp_ignored_and_cleaned_at_startup(self):
        s = spool.Spool(self.dir, 10, clock=self.clock)
        keep = s.write("t", "k", "v")
        leftover = os.path.join(self.dir, "0000000000001-00000001-abc.json.tmp")
        Path(leftover).write_text("{partial")
        self.assertEqual(s.pending(), [keep])
        spool.Spool(self.dir, 10, clock=self.clock)
        self.assertFalse(os.path.exists(leftover))

    def test_reopen_sees_same_pending(self):
        s = spool.Spool(self.dir, 10, clock=self.clock)
        names = [s.write("t", "k", str(i)) for i in range(3)]
        s.ack(names[1])
        s2 = spool.Spool(self.dir, 10, clock=self.clock)
        self.assertEqual(s2.pending(), [names[0], names[2]])

    def test_oldest_age(self):
        s = spool.Spool(self.dir, 10, clock=self.clock)
        self.assertIsNone(s.oldest_age_s(self.t[0]))
        s.write("t", "k", "v")
        self.assertAlmostEqual(s.oldest_age_s(self.t[0] + 30), 30, places=2)


if __name__ == "__main__":
    unittest.main()
