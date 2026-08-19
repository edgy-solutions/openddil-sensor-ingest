"""Thin glue: hex-encoded ASTERIX frames on stdin, decoded JSON on stdout.

DELIBERATELY MINIMAL, and the minimalism is the licence boundary. This file
is the only OpenDDIL-authored code in a GPL image, so it carries no project
internals: no ontology, no Kafka, no envelope shape beyond the record itself.
It is a decoder behind a pipe.

One line per frame in, one JSON object per frame out — so the differential
harness can drive both decoders identically and compare outputs rather than
plumbing.
"""
import json
import sys

import asterix


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = asterix.parse(bytes.fromhex(line))
            print(json.dumps({"ok": True, "records": parsed}, default=str))
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": str(exc)}))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
