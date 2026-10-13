"""Fatal-error exit, stall rule, and one-shot survival of BufferError.
Run: python -m unittest discover -s tests/spool"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from confluent_kafka import KafkaError, KafkaException  # noqa: E402

import dis_ingestor  # noqa: E402
import spool  # noqa: E402


class IsFatalTests(unittest.TestCase):
    def test_fatal_flag(self):
        self.assertTrue(dis_ingestor._is_fatal(KafkaError(KafkaError._MSG_TIMED_OUT, fatal=True)))

    def test_fatal_code_without_flag(self):
        # The case that was missed: _FATAL returned with fatal() false.
        err = KafkaError(KafkaError._FATAL, fatal=False)
        self.assertFalse(err.fatal())
        self.assertTrue(dis_ingestor._is_fatal(err))

    def test_transient_is_not_fatal(self):
        self.assertFalse(dis_ingestor._is_fatal(KafkaError(KafkaError._MSG_TIMED_OUT, fatal=False)))

    def test_none_is_not_fatal(self):
        self.assertFalse(dis_ingestor._is_fatal(None))


class ExitHookTests(unittest.TestCase):
    def test_error_cb_fatal_exits(self):
        with mock.patch.object(dis_ingestor, "_exit_process") as ex:
            dis_ingestor._error_cb(KafkaError(KafkaError._FATAL, fatal=False))
        ex.assert_called_once_with(70)

    def test_error_cb_transient_does_not_exit(self):
        with mock.patch.object(dis_ingestor, "_exit_process") as ex:
            dis_ingestor._error_cb(KafkaError(KafkaError._TRANSPORT, fatal=False))
        ex.assert_not_called()

    def test_delivery_fatal_exits(self):
        with mock.patch.object(dis_ingestor, "_exit_process") as ex:
            dis_ingestor._on_delivery(KafkaError(KafkaError._FATAL, fatal=False), None)
        ex.assert_called_once_with(70)

    def test_delivery_transient_does_not_exit(self):
        with mock.patch.object(dis_ingestor, "_exit_process") as ex:
            dis_ingestor._on_delivery(KafkaError(KafkaError._MSG_TIMED_OUT, fatal=False), None)
        ex.assert_not_called()

    def test_produce_fatal_code_exits(self):
        prod = mock.Mock()
        prod.produce.side_effect = KafkaException(KafkaError(KafkaError._FATAL, fatal=False))
        with mock.patch.object(dis_ingestor, "_producer", prod), \
                mock.patch.object(dis_ingestor, "_spool", None), \
                mock.patch.object(dis_ingestor, "_exit_process") as ex:
            dis_ingestor._publish("k", b"{}", 1)
        ex.assert_called_once_with(70)


class StallRuleTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.now = [1_000_000.0]
        self.sp = spool.Spool(self._tmp.name, 100, clock=lambda: self.now[0])
        self.sp.write("t", "k", "v")   # written at t0
        self.exits = []

    def _watch(self, metadata):
        return spool.StallWatch(self.sp, metadata, self.exits.append, stall_s=90,
                                clock=lambda: self.now[0])

    def test_old_and_broker_up_exits(self):
        w = self._watch(lambda: None)
        self.now[0] += 100
        self.assertFalse(w.check())          # first answer: one more interval
        self.now[0] += 15
        self.assertTrue(w.check())           # same entry still stuck
        self.assertEqual(self.exits, [70])

    def test_broker_back_after_long_outage_does_not_exit_at_first_answer(self):
        up = [False]

        def meta():
            if not up[0]:
                raise RuntimeError("broker down")
        w = self._watch(meta)
        self.now[0] += 100
        self.assertFalse(w.check())          # down: keep spooling
        up[0] = True
        self.now[0] += 15
        self.assertFalse(w.check())          # back, entry 115 s old: wait
        self.sp.ack(self.sp.pending()[0])    # the producer re-sent it
        self.now[0] += 15
        self.assertFalse(w.check())
        self.assertEqual(self.exits, [])

    def test_newer_oldest_entry_restarts_the_interval(self):
        w = self._watch(lambda: None)
        self.now[0] += 100
        self.sp.write("t", "k", "v2")        # written at t0+100
        self.assertFalse(w.check())
        self.sp.ack(self.sp.pending()[0])    # first delivered, second stays
        self.now[0] += 95
        self.assertFalse(w.check())          # a different oldest: one interval
        self.now[0] += 15
        self.assertTrue(w.check())
        self.assertEqual(self.exits, [70])

    def test_old_and_broker_down_keeps_running(self):
        def down():
            raise RuntimeError("broker down")
        self.now[0] += 100
        self.assertFalse(self._watch(down).check())
        self.assertEqual(self.exits, [])

    def test_young_does_not_exit(self):
        called = []
        self.now[0] += 30
        self.assertFalse(self._watch(lambda: called.append(1)).check())
        self.assertEqual(self.exits, [])
        self.assertEqual(called, [])

    def test_checks_at_most_every_15s(self):
        calls = []

        def down():
            calls.append(1)
            raise RuntimeError("down")
        w = self._watch(down)
        self.now[0] += 100
        w.check()
        self.now[0] += 5
        w.check()
        self.assertEqual(len(calls), 1)


class OneShotBufferErrorTests(unittest.TestCase):
    def test_one_shot_survives_buffer_error(self):
        with tempfile.TemporaryDirectory() as d:
            sp = spool.Spool(d, 100)
            prod = mock.Mock()
            prod.produce.side_effect = BufferError("queue full")
            with mock.patch.object(dis_ingestor, "_producer", prod), \
                    mock.patch.object(dis_ingestor, "_spool", sp), \
                    mock.patch.object(dis_ingestor, "_inflight", set()) as inflight:
                dis_ingestor._publish("k", b'{"x":1}', 2)   # Fire
                self.assertEqual(len(sp.pending()), 1)
                self.assertEqual(inflight, set())            # replayable

    def test_entity_state_not_spooled(self):
        with tempfile.TemporaryDirectory() as d:
            sp = spool.Spool(d, 100)
            prod = mock.Mock()
            with mock.patch.object(dis_ingestor, "_producer", prod), \
                    mock.patch.object(dis_ingestor, "_spool", sp):
                dis_ingestor._publish("k", b"{}", 1)
            self.assertEqual(sp.pending(), [])

    def test_delivery_acks_spool_entry(self):
        with tempfile.TemporaryDirectory() as d:
            sp = spool.Spool(d, 100)
            prod = mock.Mock()
            with mock.patch.object(dis_ingestor, "_producer", prod), \
                    mock.patch.object(dis_ingestor, "_spool", sp):
                dis_ingestor._publish("k", b"{}", 3)
                cb = prod.produce.call_args.kwargs["on_delivery"]
                self.assertEqual(len(sp.pending()), 1)
                cb(None, None)
                self.assertEqual(sp.pending(), [])

    def test_no_producer_spools_one_shot_and_drops_heartbeat(self):
        with tempfile.TemporaryDirectory() as d:
            sp = spool.Spool(d, 100)
            with mock.patch.object(dis_ingestor, "_producer", None), \
                    mock.patch.object(dis_ingestor, "_spool", sp):
                dis_ingestor._publish("k", b"{}", 21)
                dis_ingestor._publish("k", b"{}", 1)
            self.assertEqual(len(sp.pending()), 1)


if __name__ == "__main__":
    unittest.main()
