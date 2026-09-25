"""Two sidecars, one multicast group: each ingests exactly its own site.

WHAT THIS PROVES, AND WHY A COUNT IS NOT ENOUGH
-----------------------------------------------
The interesting failure is not "the filter dropped too much". It is "the
filter dropped everything", which on a quiet feed is indistinguishable from
"there was nothing to ingest". So the run is designed so that every number
is pinned from BOTH sides:

  * each sidecar must publish exactly its own site's PDUs -- a floor, so
    silence fails;
  * each sidecar must publish nothing else -- a ceiling, so a filter that
    is off fails;
  * a third site nobody claims must land nowhere at all -- the negative
    control, so a filter that accepts by accident fails.

The third site is the red check, and it runs as part of the ordinary run
rather than as a separate deliberately-broken build, because a control that
only exists when somebody remembers to run it is not a control.

Predictions are printed BEFORE anything is measured. A predicted number
written down after the fact is not a prediction.
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
GROUP = "239.10.20.30"
PORT = 62040

# site -> how many PDUs to send
PLAN = {1: 5, 2: 7, 3: 3}
TOTAL_SENT = sum(PLAN.values())

# service -> (its site, its topic)
SIDECARS = {
    "sidecar-site-1": (1, "site1-raw"),
    "sidecar-site-2": (2, "site2-raw"),
}

SETTLE_S = 12.0

_METRIC_LINE = re.compile(r"^(\S+?)(\{.*\})?\s+(\S+)$")


def compose(*args: str, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", str(HERE / "docker-compose.yml"), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def cid(service: str) -> str:
    r = compose("ps", "-q", service)
    out = r.stdout.strip()
    if not out:
        raise RuntimeError(f"no container for service {service}: {r.stderr[:200]}")
    return out.splitlines()[0]


def metrics(service: str) -> dict[str, float]:
    """Scrape one sidecar's /metrics from inside its own container.

    From inside, because the point is to read the counters of the process
    that did the filtering, not to depend on a published port the real
    deployment may not have."""
    r = subprocess.run(
        ["docker", "exec", cid(service), "python", "-c",
         "import urllib.request;"
         "print(urllib.request.urlopen('http://127.0.0.1:8080/metrics')"
         ".read().decode())"],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"metrics scrape failed for {service}: {r.stderr[:300]}")
    out: dict[str, float] = {}
    for line in r.stdout.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = _METRIC_LINE.match(line)
        if m:
            out[m.group(1) + (m.group(2) or "")] = float(m.group(3))
    return out


def topic_keys(topic: str) -> list[str]:
    """Every record key on a topic, or [] if it is empty.

    `-o :end` bounds the read at the log's current end. Sizing a read from
    `high_watermark - log_start` instead counts offsets rather than records
    and is wrong on any compacted topic -- a habit worth not forming."""
    r = subprocess.run(
        ["docker", "exec", cid("redpanda"), "rpk", "topic", "consume", topic,
         "-o", ":end", "--format", "%k" + chr(10)],
        capture_output=True, text=True, timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(f"consume {topic} failed: {r.stderr[:300]}")
    return [ln for ln in r.stdout.splitlines() if ln.strip()]


def main() -> int:
    print(__doc__)
    print("=" * 74)
    print("PREDICTIONS (written before the stack is started)")
    print("=" * 74)
    print(f"  sending on {GROUP}:{PORT} -- "
          + ", ".join(f"site {s}: {n} PDU(s)" for s, n in PLAN.items())
          + f"  (total {TOTAL_SENT})")
    print()
    for svc, (site, topic) in SIDECARS.items():
        keep = PLAN[site]
        print(f"  {svc}:")
        print(f"      dis_pdus_decoded_total        = {TOTAL_SENT}"
              "   (joined to the group, so it SEES every site)")
        print(f"      dis_pdus_filtered_total(site) = {TOTAL_SENT - keep}")
        print(f"      records on {topic:<10}       = {keep}, "
              f"every key dis:{site}:*")
    print(f"  site 3 ({PLAN[3]} PDU(s)) must appear on NEITHER topic. "
          "This is the red check.")
    print("=" * 74)
    print()

    failures: list[str] = []
    try:
        print("  bringing the stack up ...")
        r = compose("up", "-d", "--wait", timeout=600)
        if r.returncode != 0:
            print(f"FAIL: compose up: {r.stderr[-1500:]}")
            return 1

        # Joining a group is not instantaneous, and a PDU sent before the
        # join lands nowhere -- which would look exactly like the filter
        # working. Wait until both sidecars SAY they joined.
        deadline = time.time() + 90
        joined: set[str] = set()
        while time.time() < deadline and len(joined) < len(SIDECARS):
            for svc in SIDECARS:
                if svc in joined:
                    continue
                lg = subprocess.run(["docker", "logs", cid(svc)],
                                    capture_output=True, text=True, timeout=60)
                if "Joined multicast group" in (lg.stdout + lg.stderr):
                    joined.add(svc)
                    print(f"    {svc} joined {GROUP}")
            if len(joined) < len(SIDECARS):
                time.sleep(2)
        if len(joined) < len(SIDECARS):
            print(f"FAIL: only {sorted(joined)} joined the group in time")
            return 1

        print("  sending ...")
        spec = [f"{s}:{n}" for s, n in PLAN.items()]
        r = subprocess.run(
            ["docker", "exec", cid("sender"), "python", "/app/send_pdus.py",
             GROUP, str(PORT), *spec],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode != 0:
            print(f"FAIL: sender: {r.stdout[-800:]}{r.stderr[-800:]}")
            return 1
        print("    " + r.stdout.strip().replace(chr(10), chr(10) + "    "))

        print(f"  settling {SETTLE_S}s ...")
        time.sleep(SETTLE_S)

        print()
        print("=" * 74)
        print("MEASURED")
        print("=" * 74)
        for svc, (site, topic) in SIDECARS.items():
            m = metrics(svc)
            decoded = m.get("dis_pdus_decoded_total", 0.0)
            filtered = m.get('dis_pdus_filtered_total{reason="site"}', 0.0)
            keys = topic_keys(topic)
            want_keep = PLAN[site]

            print(f"  {svc}: decoded={decoded:.0f} "
                  f"filtered={filtered:.0f} records={len(keys)}")

            if decoded != TOTAL_SENT:
                failures.append(
                    f"{svc} decoded {decoded:.0f}, predicted {TOTAL_SENT}. It "
                    "is joined to the group, so it should SEE every site; a "
                    "shortfall here is the multicast join or the network, not "
                    "the filter.")
            if filtered != TOTAL_SENT - want_keep:
                failures.append(
                    f"{svc} filtered {filtered:.0f}, predicted "
                    f"{TOTAL_SENT - want_keep}")
            if len(keys) != want_keep:
                failures.append(
                    f"{topic} holds {len(keys)} record(s), predicted "
                    f"{want_keep}. Too few means the filter is eating its own "
                    "site; too many means it is not filtering.")
            wrong = sorted({k for k in keys if not k.startswith(f"dis:{site}:")})
            if wrong:
                failures.append(
                    f"{topic} holds key(s) from another site: {wrong[:5]} -- "
                    "the filter LEAKED, which on one shared group is the "
                    "failure this test exists for")

        # --- the red check, as part of the ordinary run --------------------
        print()
        strays: list[str] = []
        for svc, (_site, topic) in SIDECARS.items():
            strays += [k for k in topic_keys(topic) if k.startswith("dis:3:")]
        if strays:
            failures.append(
                "RED CHECK FAILED: site 3 was claimed by nobody, yet "
                f"{len(strays)} of its PDU(s) were published: {strays[:5]}")
            print(f"  red check: site 3 leaked onto a topic -- {strays[:5]}")
        else:
            print(f"  red check: all {PLAN[3]} site-3 PDU(s) landed nowhere, "
                  "as predicted")

    finally:
        print()
        print("  tearing the stack down ...")
        compose("down", "-v", timeout=300)

    print()
    if failures:
        print(f"FAIL: multicast site filter - {len(failures)} violation(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS: multicast site filter - two sidecars on one group, each "
          f"ingested exactly its own site ({PLAN[1]} and {PLAN[2]}), each saw "
          f"all {TOTAL_SENT}, and the {PLAN[3]} unclaimed PDU(s) landed "
          "nowhere.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
