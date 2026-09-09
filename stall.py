"""In-process stall detector, wired to LIVENESS.

WHY LIVENESS AND NOT READINESS
------------------------------
Readiness removes a pod from Service endpoints. For a Kafka client that means
nothing: nobody routes to it, and the pod that is stuck stays stuck. The
remedy for a wedge is a RESTART, and restart is what liveness does.

Getting this backwards ships a probe that flips endpoints nobody reads while
the wedged process runs forever — a check that appears to act and does not,
which is this project's most-recorded failure shape wearing a Kubernetes
object.

So: readiness keeps its existing meaning ("connected to my broker"), and the
stall condition gates liveness.

WHY IN-PROCESS
--------------
The component already knows its own progress: it counts what it receives and
its delivery callbacks say what was published. Keeping a monotonic
last-input and last-output timestamp in memory puts the state where the
knowledge already is, and leaves the kubelet's probe fast and stateless.

The alternatives were considered and rejected. A sidecar adds a component
that can itself be wrong — one more thing at 1/1 Running doing nothing. An
exec probe querying broker offsets makes LIVENESS depend on a broker call:
heavier, and it restarts the pod when the broker is briefly unreachable,
which is the opposite of what should happen.

THE CONDITION, AND THE TERM THAT KEEPS IT HONEST
------------------------------------------------
    stalled  ==  input advanced over the window  AND  output did not

Both terms are required. "Output did not advance" alone fires on an idle
input — a quiet fleet, a declared-idle family — and a liveness probe that
restarts a healthy pod every window is a worse outage than the wedge it was
added to catch.

RELAYS NEED A THIRD TERM (see the module note in the chart): under severance
a relay's output legitimately stops, because buffering IS the designed
degraded mode. A plain stall probe would restart the bridge every window for
the duration of a cut — failing closed across a link that is expected to
fail, arriving as a liveness probe. For relays the condition gains
`destination reachable`, and the must-not-fire cases matter as much as the
fire case.

THRESHOLDS ARE MINUTES, NOT SECONDS, and are chart values. A flapping
liveness probe is a worse outage than a wedge, so the default errs long: a
wedge that persists for minutes is still caught long before a person would
notice, and a slow window costs nothing.
"""
from __future__ import annotations

import os
import threading
import time

# Window over which progress is judged. Minutes by default, deliberately.
STALL_WINDOW_S = float(os.getenv("STALL_WINDOW_S", "180"))

# Grace after start before the detector may report stalled at all. A process
# that is still connecting has not stalled, and restarting it during startup
# would make a crash loop out of a slow broker.
STALL_GRACE_S = float(os.getenv("STALL_GRACE_S", "120"))

_lock = threading.Lock()
_started = time.monotonic()
_last_input = 0.0
_last_output = 0.0


def note_input() -> None:
    """Called when a message is received/decoded — evidence of live input."""
    global _last_input
    with _lock:
        _last_input = time.monotonic()


def note_output() -> None:
    """Called on CONFIRMED delivery, not on produce().

    The distinction is the point: `produce()` enqueues locally and succeeds
    while a producer is disconnected, so counting attempts would report
    progress precisely when there is none.
    """
    global _last_output
    with _lock:
        _last_output = time.monotonic()


def state() -> dict:
    now = time.monotonic()
    with _lock:
        li, lo = _last_input, _last_output
    up = now - _started
    in_age = (now - li) if li else None
    out_age = (now - lo) if lo else None

    if up < STALL_GRACE_S:
        return {"stalled": False, "reason": "grace", "uptime_s": round(up, 1)}

    input_live = in_age is not None and in_age <= STALL_WINDOW_S
    output_live = out_age is not None and out_age <= STALL_WINDOW_S

    if input_live and not output_live:
        return {
            "stalled": True,
            "reason": "input advanced, output did not",
            "input_age_s": round(in_age, 1),
            "output_age_s": None if out_age is None else round(out_age, 1),
            "window_s": STALL_WINDOW_S,
        }
    # NOT STALLED when the input is idle. A quiet source is not a broken
    # process, and this is the branch that keeps the probe from restarting
    # healthy pods on a fleet that has nothing to say.
    return {
        "stalled": False,
        "reason": "ok" if output_live else "input idle",
        "input_age_s": None if in_age is None else round(in_age, 1),
        "output_age_s": None if out_age is None else round(out_age, 1),
    }
