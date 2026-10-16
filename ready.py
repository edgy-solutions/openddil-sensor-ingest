"""In-process readiness tracker, wired to READINESS.

THE CONDITION
-------------
    ready  ==  socket opened less than the window ago
           OR  an Entity State PDU for this sidecar's site was accepted
               less than the window ago

The second term is the signal: the co-located simulator is running and
this sidecar is receiving its own site. The first term is the allowance.
Before the simulator is run it emits control traffic only, no entity
PDUs, so a sidecar that has just started has nothing to show yet and
should not be marked unready for it.

The allowance is finite on purpose. A sidecar that has seen no entity
PDU for a whole window after the allowance -- wrong group, wrong site,
simulator never run -- reports not ready, and that is the answer.

WHAT IT GATES
-------------
By default, nothing. /healthz/ready answers 200 always, and the real state
rides in the JSON body ("ready", "gates_pod" and the counters) and in the
dis_sidecar_ready gauge (0/1); the gauge is the alerting signal.

Readiness is per POD, not per container: a sidecar answering 503 to a
readinessProbe takes the whole pod, the simulator included, out of every
Service that selects it. A sidecar that cannot hear its simulator should
raise an alert, not remove the simulator from the network, and a site that
wired this probe in its own recipe must not have its simulator pulled out
of its Services by a sidecar's opinion.

READY_GATES_POD=1 restores the gate (503 when not ready) for a pod that has
no simulator behind a Service, where pulling the pod out is the point.

The window is measured from when the UDP socket opens, not from import;
see note_started().
"""
from __future__ import annotations

import os
import threading
import time

from prometheus_client import Gauge

# Before the simulator is run it emits no entity PDUs. The allowance is how
# long that is tolerated after the socket opens, and how long a gap in
# entity PDUs is tolerated after that (see the module note).
READY_ENTITY_WINDOW_S = float(os.getenv("READY_ENTITY_WINDOW_S", "90"))

# 1 restores the old gate: /healthz/ready answers 503 when not ready.
READY_GATES_POD = os.getenv("READY_GATES_POD", "0").strip() == "1"

DIS_SIDECAR_READY = Gauge(
    "dis_sidecar_ready",
    "1 when the readiness condition holds (see ready.py), else 0. The "
    "alerting signal; /healthz/ready no longer gates the pod by default.")

_lock = threading.Lock()
_started = time.monotonic()
_last_entity = 0.0
_entity_count = 0


def note_started() -> None:
    """Reset the readiness clock to now.

    Must be called explicitly at the moment the UDP socket is opened/bound
    in dis_ingestor.run() -- module import happens well before the socket
    is opened, so the module-level default above is only a fallback (e.g.
    for tests that call readiness()/state() without ever calling this).
    """
    global _started
    with _lock:
        _started = time.monotonic()


def note_entity() -> None:
    """Called when an EntityStatePdu is accepted for publishing."""
    global _last_entity, _entity_count
    with _lock:
        _entity_count += 1
        _last_entity = time.monotonic()
    DIS_SIDECAR_READY.set(1)        # an accepted ES is, by definition, ready


def readiness(now: float, started: float, last_entity: float, count: int, window: float) -> dict:
    age = (now - last_entity) if last_entity else None
    ready = (now - started) < window or (age is not None and age <= window)
    return {
        "ready": ready,
        "entity_pdus": count,
        "last_entity_age_s": None if age is None else round(age, 1),
        "uptime_s": round(now - started, 1),
        "window_s": window,
    }


def state() -> dict:
    with _lock:
        started, last_entity, count = _started, _last_entity, _entity_count
    return readiness(time.monotonic(), started, last_entity, count, READY_ENTITY_WINDOW_S)


def probe() -> tuple[int, dict]:
    """The /healthz/ready answer: (HTTP status, JSON body).

    Updates the gauge on every call. 200 always unless READY_GATES_POD, in
    which case 503 when not ready; "gates_pod" in the body says which.
    """
    rd = state()
    DIS_SIDECAR_READY.set(1 if rd["ready"] else 0)
    rd["gates_pod"] = READY_GATES_POD
    return (200 if (rd["ready"] or not READY_GATES_POD) else 503), rd
