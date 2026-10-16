"""
/healthz/ready answers 200 always unless READY_GATES_POD=1, and the real
state rides in the body and in the dis_sidecar_ready gauge.

Pure unit tests: ready.probe() is the handler's whole decision, called
directly, with the clock fields patched rather than slept on.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import ready  # noqa: E402


def _not_ready():
    return (mock.patch.object(ready, "_started", time.monotonic() - 1000.0),
            mock.patch.object(ready, "_last_entity", 0.0))


def _ready_now():
    return (mock.patch.object(ready, "_started", time.monotonic()),
            mock.patch.object(ready, "_last_entity", 0.0))


def _gauge():
    return ready.DIS_SIDECAR_READY._value.get()


def test_default_not_ready_answers_200_with_gates_pod_false():
    a, b = _not_ready()
    with a, b, mock.patch.object(ready, "READY_GATES_POD", False):
        status, body = ready.probe()
    assert status == 200
    assert body["ready"] is False
    assert body["gates_pod"] is False


def test_gate_on_not_ready_answers_503():
    a, b = _not_ready()
    with a, b, mock.patch.object(ready, "READY_GATES_POD", True):
        status, body = ready.probe()
    assert status == 503
    assert body["ready"] is False


def test_gate_on_ready_answers_200():
    a, b = _ready_now()
    with a, b, mock.patch.object(ready, "READY_GATES_POD", True):
        status, body = ready.probe()
    assert status == 200 and body["ready"] is True


def test_gauge_follows_probe():
    a, b = _not_ready()
    with a, b:
        ready.probe()
        assert _gauge() == 0
    a, b = _ready_now()
    with a, b:
        ready.probe()
        assert _gauge() == 1


def test_gauge_set_on_accepted_entity():
    a, b = _not_ready()
    with a, b:
        ready.probe()
        assert _gauge() == 0
        ready.note_entity()
        assert _gauge() == 1
