"""
Synthetic ASTERIX frame builder — authored test input, stamped as such.

WHY THIS EXISTS. The acceptance test for the ASTERIX sidecar needs frames.
Real captures exist but ship inside a GPL-licensed package, so they cannot be
committed to an MIT repository; and a test that depends on a file nobody may
redistribute is a test that stops working for the next person.

These frames are FABRICATED. The coordinates are arbitrary. Nothing here is a
recording of anything, and no value should be read as characteristic of a real
sensor — the same discipline dis-sim carries (ADR-0017: authored input
self-identifies).

WHAT MAKES IT VERIFICATION RATHER THAN CIRCULAR REASONING. The bytes are
built here and decoded by `asterix4py`, which is third-party code this project
did not write. A generator checked by its own parser proves nothing; a
generator checked by an independent decoder proves the bytes are on-spec.
Both sides read the same category XML — that is the specification, and
sharing it is the point rather than a flaw.

Encoding per EUROCONTROL ASTERIX CAT062 (edition 1.18), the fields taken from
the category definition rather than from memory:

    block  = CAT(1) | LEN(2, big-endian, total incl. header) | record...
    record = FSPEC(1+) | items in UAP order

    FRN 1 -> I062/010  Data Source Identifier   2 bytes  SAC, SIC
    FRN 3 -> I062/015  Service Identification    1 byte
    FRN 4 -> I062/070  Time Of Track Information 3 bytes  LSB 1/128 s
    FRN 5 -> I062/105  Position in WGS-84        8 bytes  LSB 180/2^25 deg
"""
from __future__ import annotations

import struct

CAT062 = 62

# 180/2^25 degrees per LSB — the WGS-84 resolution CAT062 I062/105 declares.
_WGS84_LSB = 180.0 / (1 << 25)
# I062/070 Time Of Track Information: 1/128 s since midnight UTC.
_TOD_LSB = 1.0 / 128.0

# FSPEC bit for each field reference number, MSB first, bit 1 reserved for FX.
_FRN_BIT = {1: 0x80, 2: 0x40, 3: 0x20, 4: 0x10, 5: 0x08, 6: 0x04, 7: 0x02}
_FX_BIT = 0x01
# Second FSPEC octet carries FRN 8..14, same MSB-first layout.
_FRN2_BIT = {8: 0x80, 9: 0x40, 10: 0x20, 11: 0x10, 12: 0x08, 13: 0x04, 14: 0x02}


def _twos_complement_32(value: int) -> bytes:
    """CAT062 lat/lon are signed 32-bit, two's complement."""
    return struct.pack(">i", value)


def cat062_record(sac: int, sic: int, sid: int,
                  time_of_track_s: float,
                  lat_deg: float, lon_deg: float,
                  track_number: int | None = None) -> bytes:
    """One CAT062 record carrying I062/010, /015, /070, /105 and optionally
    /040 (track number).

    Track number is FRN 12, which lives in the SECOND FSPEC octet — so
    including it exercises FSPEC extension (the FX bit), the part of ASTERIX
    framing most likely to be wrong in a hand-rolled encoder and the reason
    ADR-0030 keeps this grammar out of Bloblang.
    """
    fspec1 = _FRN_BIT[1] | _FRN_BIT[3] | _FRN_BIT[4] | _FRN_BIT[5]

    i010 = bytes((sac & 0xFF, sic & 0xFF))
    i015 = bytes((sid & 0xFF,))

    tot = int(round(time_of_track_s / _TOD_LSB)) & 0xFFFFFF
    i070 = bytes(((tot >> 16) & 0xFF, (tot >> 8) & 0xFF, tot & 0xFF))

    i105 = (_twos_complement_32(int(round(lat_deg / _WGS84_LSB)))
            + _twos_complement_32(int(round(lon_deg / _WGS84_LSB))))

    if track_number is None:
        return bytes((fspec1,)) + i010 + i015 + i070 + i105

    # FX on octet 1, FRN 12 (bit 4) on octet 2. Items stay in UAP order.
    fspec1 |= _FX_BIT
    fspec2 = _FRN2_BIT[12]
    i040 = struct.pack(">H", track_number & 0xFFFF)
    return bytes((fspec1, fspec2)) + i010 + i015 + i070 + i105 + i040


def cat062_block(records: list[bytes]) -> bytes:
    """Wrap records in an ASTERIX data block. LEN counts the whole block."""
    body = b"".join(records)
    return struct.pack(">BH", CAT062, len(body) + 3) + body


def sample_track_block(track_lat: float = 51.5,
                       track_lon: float = -0.125,
                       sac: int = 200, sic: int = 1) -> bytes:
    """One-record block with obviously-synthetic defaults.

    SAC 200 is deliberately outside the ranges real allocations use, so a
    frame that escapes into a real feed is identifiable as ours rather than
    plausible. Same reasoning as dis-sim's fictional entity ids.
    """
    return cat062_block([
        cat062_record(sac=sac, sic=sic, sid=1,
                      time_of_track_s=43200.0,
                      lat_deg=track_lat, lon_deg=track_lon,
                      track_number=4242),
    ])
