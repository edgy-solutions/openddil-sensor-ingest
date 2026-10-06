"""
Unit test for dis_ingestor._open_multicast_socket()'s SO_REUSEPORT bind.

PURE UNIT TEST, NO INFRASTRUCTURE, NO MULTICAST TRAFFIC. This proves only
that a second bind on the same port succeeds when the first socket set
SO_REUSEPORT and the second (dis_ingestor's own) sets both SO_REUSEADDR and
SO_REUSEPORT -- the co-located-simulator scenario _open_multicast_socket's
own docstring describes, where the other process on the port may have set
only one of the two.

The same-uid rule is Linux semantics (BSD differs; Windows has no
SO_REUSEPORT), so this test
skips off Linux -- see dis_ingestor._open_multicast_socket, which
degrades the same way via hasattr().

Run with: python -m unittest discover -s tests/multicast_reuseport
(or via pytest, if available -- no pytest-only features are used).
"""
from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import dis_ingestor  # noqa: E402


@unittest.skipUnless(
    sys.platform.startswith("linux") and hasattr(socket, "SO_REUSEPORT"),
    "Linux SO_REUSEPORT semantics",
)
class ReusePortBindTests(unittest.TestCase):
    def test_second_bind_succeeds_with_reuseport_on_both_sides(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("0.0.0.0", 0))
        port = probe.getsockname()[1]
        probe.close()

        first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        second = None
        try:
            # Only SO_REUSEPORT, deliberately -- the co-located simulator
            # may set only one of the two reuse options, not both.
            first.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            first.bind(("0.0.0.0", port))

            second = dis_ingestor._open_multicast_socket(
                "239.1.2.3", "0.0.0.0", port, 1024 * 1024,
            )
        finally:
            first.close()
            if second is not None:
                second.close()


if __name__ == "__main__":
    unittest.main()
