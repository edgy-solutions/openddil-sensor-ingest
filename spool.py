"""
Durable spool for one-shot PDUs, and the stall rule that reads it.

WHY THIS EXISTS. Entity State is a heartbeat: if one is lost, the next one
heals it. Fire, Detonation, Event Report, Remove Entity and Resupply Received
are sent once and never again, so a lost one is lost for good. Those records
are written to disk BEFORE they are handed to the Kafka producer and removed
only when the broker confirms delivery. Whatever is still on disk after a
crash, a broker outage or a producer failure is produced again on the next
start, and again periodically while running.

DELIVERY IS AT-LEAST-ONCE. A record delivered just before a crash, whose
ack (the unlink) had not yet happened, is sent a second time on replay.
Downstream keys Fire and Resupply rows on the event id, so a duplicate is
idempotent there.

One file per record, named `<13-digit ms epoch>-<seq:08d>-<uuid4 hex>.json`,
so a plain sort of the names is age order. A record is written to
`<name>.tmp`, fsynced, then renamed: a reader never sees a partial file, and a
`.tmp` left by a crash is ignored and removed at startup.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Callable

from prometheus_client import Counter

logger = logging.getLogger("dis_ingestor.spool")

# Every published PDU type except Entity State (1). Entity State is a
# heartbeat -- the next one supersedes a lost one -- so spooling it would
# only write the disk at the full feed rate for no gain. Fire (2),
# Detonation (3), Resupply Received (7), Remove Entity (12) and Event
# Report (21) are sent once and never again.
ONE_SHOT_PDU_TYPES = frozenset({2, 3, 7, 12, 21})

DIS_SPOOL_DROPPED = Counter(
    "dis_spool_dropped_total",
    "One-shot records dropped from the spool because it was full "
    "(the oldest entry is dropped to keep the newest)",
)


class Spool:
    def __init__(self, directory: str, max_entries: int,
                 clock: Callable[[], float] = time.time) -> None:
        self.directory = directory
        self.max_entries = max_entries
        self._clock = clock
        self._seq = 0
        self._lock = threading.Lock()
        self.dropped = 0
        os.makedirs(directory, exist_ok=True)
        for fn in os.listdir(directory):
            if fn.endswith(".tmp"):
                try:
                    os.unlink(os.path.join(directory, fn))
                except OSError:
                    pass

    def pending(self) -> list[str]:
        """Pending entry names, oldest first."""
        return sorted(fn for fn in os.listdir(self.directory)
                      if fn.endswith(".json"))

    def write(self, topic: str, key: str | None, value: str) -> str:
        with self._lock:
            names = self.pending()
            while len(names) >= self.max_entries > 0:
                oldest = names.pop(0)
                self._unlink(oldest)
                self.dropped += 1
                DIS_SPOOL_DROPPED.inc()
                logger.error("Spool full (%d entries): dropped oldest %s",
                             self.max_entries, oldest)
            self._seq += 1
            name = "%013d-%08d-%s.json" % (int(self._clock() * 1000),
                                           self._seq, uuid.uuid4().hex)
            path = os.path.join(self.directory, name)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"topic": topic, "key": key, "value": value}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            return name

    def read(self, name: str) -> dict:
        with open(os.path.join(self.directory, name), encoding="utf-8") as fh:
            return json.load(fh)

    def ack(self, name: str) -> None:
        self._unlink(name)

    def quarantine(self, name: str) -> None:
        """Set an unreadable entry aside so it is not retried forever."""
        try:
            os.replace(os.path.join(self.directory, name),
                       os.path.join(self.directory, name + ".bad"))
        except OSError:
            pass

    def _unlink(self, name: str) -> None:
        try:
            os.unlink(os.path.join(self.directory, name))
        except FileNotFoundError:
            pass

    @staticmethod
    def age_s(name: str, now: float) -> float:
        return now - int(name[:13]) / 1000.0

    def oldest_age_s(self, now: float) -> float | None:
        names = self.pending()
        return self.age_s(names[0], now) if names else None


class StallWatch:
    """Exit when the broker is up but the oldest spooled record is stuck.

    A wedged client: the broker answers metadata and we still cannot deliver.
    If the metadata request fails the broker is down; keep running and
    spooling, because exiting would only add a restart gap in which nothing
    listens.

    ONE GOOD ANSWER IS NOT ENOUGH. After an outage longer than stall_s, the
    oldest entry is already past the threshold at the first check that finds
    the broker back, while a healthy producer is still reconnecting and has
    not yet re-sent it. Exiting there would restart a client that was about
    to recover, and the restart gap loses the PDUs that arrive in it. So the
    same oldest entry must still be pending at a check that follows an
    earlier check where the broker also answered: one full check interval
    with the broker up and nothing delivered.
    """

    def __init__(self, spool: Spool, metadata: Callable[[], object],
                 exit_fn: Callable[[int], None], stall_s: float = 90.0,
                 check_every_s: float = 15.0, warn_every_s: float = 60.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.spool = spool
        self.metadata = metadata
        self.exit_fn = exit_fn
        self.stall_s = stall_s
        self.check_every_s = check_every_s
        self.warn_every_s = warn_every_s
        self._clock = clock
        self._last_check: float | None = None
        self._last_warn: float | None = None
        # The oldest pending name at the previous check where the broker
        # answered; None after any check that did not reach the broker.
        self._stuck_name: str | None = None

    def check(self) -> bool:
        """Returns True if it decided to exit."""
        now = self._clock()
        if self._last_check is not None and now - self._last_check < self.check_every_s:
            return False
        self._last_check = now
        names = self.spool.pending()
        if not names:
            self._stuck_name = None
            return False
        oldest = names[0]
        age = self.spool.age_s(oldest, now)
        if age <= self.stall_s:
            self._stuck_name = None
            return False
        try:
            self.metadata()
        except Exception as exc:  # broker down: keep spooling
            self._stuck_name = None
            if self._last_warn is None or now - self._last_warn >= self.warn_every_s:
                self._last_warn = now
                logger.warning("Oldest spooled record is %.0f s old and the "
                               "broker is not answering (%s); still spooling.",
                               age, exc)
            return False
        if self._stuck_name != oldest:
            # First check with the broker up for this entry: give the
            # producer one interval to deliver it.
            self._stuck_name = oldest
            logger.warning("Oldest spooled record is %.0f s old and the broker "
                           "answers; exiting if it is still pending in %.0f s.",
                           age, self.check_every_s)
            return False
        logger.critical("Oldest spooled record is %.0f s old although the "
                        "broker answers metadata: the producer is wedged. "
                        "Exiting so the pod restarts.", age)
        self.exit_fn(70)
        return True
