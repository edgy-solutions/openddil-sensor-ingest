"""
Differential harness: MIT decoder vs GPL reference, over identical frames.

WHY. `asterix4py` (MIT, pure Python) is what ships. `asterix-decoder`
(GPL, C extension) is field-proven and independent — different authors,
different implementation language, no shared upstream. Agreement between two
implementations that share nothing but the specification is real evidence;
a decoder checked only against the definitions it reads is close to none.

WHAT IS COMPARED, AND WHAT DELIBERATELY IS NOT. Only the fields the Silver
mapping consumes: SAC/SIC, track number, WGS-84 lat/lon. Full-output
equality is EXPLICITLY NOT the goal — the two produce different shapes by
design (the GPL one nests `{"desc": ..., "val": ...}` and adds CRC and
timestamps), and chasing byte-equality would mean tuning the comparison
until it passed, which is the fitted-check failure mirrored.

So: disagreement inside the consumed set is a FINDING. Difference outside it
is an OBSERVATION, reported and not failed on.

    python tools/asterix_differential.py            # synthetic frames
    python tools/asterix_differential.py --bench    # add decode-rate numbers

Requires the GPL image to be built:
    docker build -t openddil/asterix-reference:gpl asterix-reference/
If it is absent the harness runs MIT-side only and SAYS SO — a one-sided run
is not a differential result and must not read like one.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fixtures.asterix_frames import cat062_block, cat062_record  # noqa: E402

GPL_IMAGE = "openddil/asterix-reference:gpl"

# The consumed set. Kept in one place so the harness cannot silently drift
# from the mapping it is defending.
CONSUMED = ("sac", "sic", "trk", "lat", "lon")


def build_frames() -> list[bytes]:
    """Synthetic CAT062 frames spanning the coordinate edges that matter."""
    cases = [
        (51.5, -0.125, 4242),        # ordinary
        (0.0, 0.0, 1),               # null island — both signs zero
        (-33.8688, 151.2093, 65535), # southern + eastern, max track number
        (44.7344, 13.0415, 4980),    # matches the real capture's values
        (-45.0, 179.9, 100),         # near the antimeridian
        (89.9, -179.9, 7),           # near the poles, opposite sign
    ]
    return [cat062_block([cat062_record(200, 1, 1, 43200.0, lat, lon, track_number=t)])
            for lat, lon, t in cases]


def normalise_mit(record: dict) -> dict:
    return {
        "sac": (record.get("010") or {}).get("SAC"),
        "sic": (record.get("010") or {}).get("SIC"),
        "trk": (record.get("040") or {}).get("TrkN"),
        "lat": (record.get("105") or {}).get("Lat"),
        "lon": (record.get("105") or {}).get("Lon"),
    }


def _val(node, key):
    """GPL output nests every field as {'desc': ..., 'val': ...}."""
    inner = (node or {}).get(key)
    return inner.get("val") if isinstance(inner, dict) else inner


def normalise_gpl(record: dict) -> dict:
    return {
        "sac": _val(record.get("I010"), "SAC"),
        "sic": _val(record.get("I010"), "SIC"),
        "trk": _val(record.get("I040"), "TrkN"),
        "lat": _val(record.get("I105"), "Lat"),
        "lon": _val(record.get("I105"), "Lon"),
    }


def decode_mit(frames: list[bytes]) -> list[dict]:
    from asterix4py import AsterixParser
    out = []
    for f in frames:
        res = AsterixParser(f).get_result()
        out.append(normalise_mit(next(iter(res.values()))))
    return out


def decode_gpl(frames: list[bytes]) -> list[dict] | None:
    if shutil.which("docker") is None:
        return None
    probe = subprocess.run(["docker", "image", "inspect", GPL_IMAGE],
                           capture_output=True)
    if probe.returncode != 0:
        return None
    payload = "\n".join(f.hex() for f in frames) + "\n"
    proc = subprocess.run(["docker", "run", "--rm", "-i", GPL_IMAGE],
                          input=payload, capture_output=True, text=True)
    out = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        doc = json.loads(line)
        if not doc.get("ok"):
            out.append({"error": doc.get("error")})
            continue
        out.append(normalise_gpl(doc["records"][0]))
    return out


def benchmark(frames: list[bytes], rounds: int = 200) -> dict:
    """Decode rate per implementation. Counts reported, not just a verdict.

    The GPL side is measured IN-CONTAINER over a batch, so the number is
    decode throughput rather than container startup — one `docker run` for
    the whole batch, the same way the harness drives it.
    """
    from asterix4py import AsterixParser
    batch = frames * rounds

    t0 = time.perf_counter()
    for f in batch:
        AsterixParser(f).get_result()
    mit_s = time.perf_counter() - t0

    result = {
        "frames": len(batch),
        "mit_seconds": round(mit_s, 3),
        "mit_frames_per_s": round(len(batch) / mit_s, 1),
    }

    probe = subprocess.run(["docker", "image", "inspect", GPL_IMAGE], capture_output=True)
    if probe.returncode == 0:
        payload = "\n".join(f.hex() for f in batch) + "\n"
        t0 = time.perf_counter()
        subprocess.run(["docker", "run", "--rm", "-i", GPL_IMAGE],
                       input=payload, capture_output=True, text=True)
        gpl_s = time.perf_counter() - t0
        result["gpl_seconds_incl_container"] = round(gpl_s, 3)
        result["gpl_frames_per_s"] = round(len(batch) / gpl_s, 1)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", action="store_true", help="also report decode rates")
    args = ap.parse_args()

    frames = build_frames()
    mit = decode_mit(frames)
    gpl = decode_gpl(frames)

    if gpl is None:
        print("MIT decoder only — the GPL reference image is not available.")
        print("THIS IS NOT A DIFFERENTIAL RESULT. Build it with:")
        print("  docker build -t openddil/asterix-reference:gpl asterix-reference/")
        for i, m in enumerate(mit):
            print(f"  frame {i}: {m}")
        return 0

    findings = 0
    for i, (m, g) in enumerate(zip(mit, gpl)):
        diffs = [k for k in CONSUMED if m.get(k) != g.get(k)]
        if diffs:
            findings += 1
            print(f"  FINDING frame {i}: decoders disagree on {diffs}")
            print(f"          mit={{{', '.join(f'{k}={m.get(k)!r}' for k in diffs)}}}")
            print(f"          gpl={{{', '.join(f'{k}={g.get(k)!r}' for k in diffs)}}}")
        else:
            print(f"  ok      frame {i}: agree on {len(CONSUMED)} consumed fields "
                  f"(trk={m['trk']}, lat={m['lat']:.6f}, lon={m['lon']:.6f})")

    print()
    print(f"{len(frames)} frames, {len(CONSUMED)} consumed fields each, "
          f"{findings} disagreement(s)")
    print("Shape differences outside the consumed set are expected and not "
          "compared — see the module docstring.")

    if args.bench:
        print()
        for k, v in benchmark(frames).items():
            print(f"  {k}: {v}")

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
