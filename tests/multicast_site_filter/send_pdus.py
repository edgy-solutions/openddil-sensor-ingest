"""Emit DIS Entity State PDUs to a multicast group, tagged by site.

Runs INSIDE the compose network, because multicast has to cross the bridge
for this test to mean anything. Sending from the host would either not reach
the containers at all or would reach them by a path the real deployment does
not use.

Usage:  python send_pdus.py <group> <port> <site>:<count> [<site>:<count> ...]
"""
from __future__ import annotations

import socket
import sys
import time

from io import BytesIO

from opendis.dis7 import EntityStatePdu
from opendis.DataOutputStream import DataOutputStream


def build(site: int, entity: int) -> bytes:
    """One Entity State PDU for (site, application=1, entity).

    Deliberately minimal. This test is about which sidecar a PDU reaches,
    not about field fidelity -- the fields that matter here are the three
    that make up the entity id, and marking, which makes a record readable
    when a human has to look at one.
    """
    pdu = EntityStatePdu()
    # These header fields have no usable defaults in opendis -- serialize()
    # raises struct.error on the unset pduStatus. fixtures/generate_fixtures.py
    # sets the same set, and this mirrors it deliberately rather than
    # inventing a second idea of what a minimal PDU is.
    pdu.protocolVersion = 7
    pdu.exerciseID      = 1
    pdu.pduType         = 1   # Entity State
    pdu.protocolFamily  = 1   # Entity Information / Interaction
    pdu.pduStatus       = 0
    pdu.entityAppearance = 0
    pdu.capabilities    = 0

    pdu.entityID.siteID = site
    pdu.entityID.applicationID = 1
    pdu.entityID.entityID = entity
    pdu.entityType.entityKind = 1     # Platform
    pdu.entityType.domain = 1         # Land
    pdu.entityType.country = 225      # USA
    pdu.entityType.category = 1
    pdu.entityType.subcategory = 1
    pdu.entityType.specific = 18
    pdu.entityType.extra = 0
    pdu.forceId = 1
    pdu.entityLocation.x = 1000.0 + entity
    pdu.entityLocation.y = 2000.0 + entity
    pdu.entityLocation.z = 3000.0
    label = f"S{site}-{entity}".encode("ascii")[:11]
    pdu.marking.characters = list(label.ljust(11, bytes([0])))

    buf = BytesIO()
    pdu.serialize(DataOutputStream(buf))
    return buf.getvalue()


def main() -> int:
    group, port = sys.argv[1], int(sys.argv[2])
    plan = [(int(s.split(":")[0]), int(s.split(":")[1])) for s in sys.argv[3:]]

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # TTL 2: enough to cross the docker bridge, not enough to leave a host.
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

    total = 0
    for site, count in plan:
        for i in range(count):
            sock.sendto(build(site, 1000 + i), (group, port))
            total += 1
            # UDP has no backpressure and a burst can overrun the receive
            # buffer, which would make a filter bug and a dropped datagram
            # look identical. A few milliseconds apart costs nothing here.
            time.sleep(0.02)
        print(f"sent {count} PDU(s) for site {site}", flush=True)

    print(f"TOTAL SENT {total}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
