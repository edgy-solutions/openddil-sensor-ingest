"""
DIS entity-appearance decoding — Stage 1, ontology-driven (ADR-0030).

Turns the 32-bit `entityAppearance` field into named facts. Lives in the
sidecar because bit extraction is a WIRE GRAMMAR, and ADR-0013 keeps
arithmetic out of mappings; the INTERPRETATION lives in the ontology
(`dis_appearance.yaml`) because it varies per entity kind and domain and is
curated by PR, exactly as platform-variant resolution is (ADR-0016).

TWO REFUSALS ARE THE POINT OF THIS MODULE, and both exist because the same
bit pattern means two different things:

  1. AN UNDECLARED SOURCE IS NOT DECODED. An all-zero appearance field is
     indistinguishable on the wire from a producer that never sets it, and
     bits 3-4 of zero read as damage NONE — a POSITIVE claim of health.
     Reading an undeclared source would manufacture "healthy" out of
     silence. Sources declare themselves in the ontology, with an author and
     a date; absent that, this returns nothing and the axis stays
     UNSPECIFIED.

  2. A DOMAIN WITHOUT A FIELD DOES NOT GET ONE. Bit 2 is firepower-kill for
     land platforms and carries an unrelated meaning elsewhere, so the air
     block simply omits it. Absence in the table is an instruction, not an
     oversight (ADR-0026 amendment, clause 3).

Returns a dict of only what the source actually said. Callers must treat a
missing key as "no claim", never as a negative.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("dis_ingestor.appearance")

_ONTOLOGY_DIR = os.getenv("ONTOLOGY_DIR", "/ontology")
_TABLE: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _TABLE
    if _TABLE is not None:
        return _TABLE
    path = Path(_ONTOLOGY_DIR) / "dis_appearance.yaml"
    try:
        import yaml  # noqa: PLC0415
        with path.open(encoding="utf-8") as fh:
            _TABLE = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        # Not an error. A deployment that has not mounted the table decodes
        # nothing, which is the same honest outcome as an undeclared source.
        logger.info("No dis_appearance.yaml at %s — appearance not decoded", path)
        _TABLE = {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load %s (%s) — appearance not decoded", path, exc)
        _TABLE = {}
    return _TABLE


def source_declared(site_id: int) -> bool:
    """Has this DIS site been DECLARED to populate appearance?"""
    return str(site_id) in (_load().get("populating_sources") or {})


def decode(appearance_bits: int, kind: int, domain: int, site_id: int) -> dict[str, Any]:
    """Decode appearance into named facts, or return {} if we must not.

    Empty dict means "no claim was read" — from an undeclared source, an
    unmapped kind/domain, or a missing table. It never means "no damage".
    """
    if not source_declared(site_id):
        return {}

    # THE ZERO GUARD, and it closes a hole the declaration alone does not.
    # Declaration is per SOURCE; emission can be per RUN. dis-sim is declared,
    # but only emits appearance when asked (`--damage`) — so a declared
    # generator running without the flag would otherwise decode to
    # "damage NONE, power plant off", a false claim manufactured from a field
    # it deliberately left alone.
    #
    # An entity making ANY claim sets the power-plant bit, so a genuine claim
    # is never all-zero. Exactly zero therefore means nothing was said.
    #
    # The one ambiguity is named rather than hidden: a powered-OFF, undamaged
    # entity also encodes to zero and is read here as silence. That is the
    # safe direction to be wrong — it withholds a claim rather than inventing
    # one — but it IS a limitation, not a proof.
    if appearance_bits == 0:
        return {}
    block = (_load().get("appearance") or {}).get(f"{kind}_{domain}")
    if not block:
        return {}

    out: dict[str, Any] = {}

    dmg = block.get("damage")
    if dmg:
        lo, hi = dmg["bits"][0], dmg["bits"][1]
        width = hi - lo + 1
        raw = (appearance_bits >> lo) & ((1 << width) - 1)
        # An unmapped code is NOT silently dropped: the producer said
        # something we do not understand, which is a different fact from
        # saying nothing (AUDIT-2026-08-15 F1).
        name = (dmg.get("values") or {}).get(raw)
        out["damage"] = name if name is not None else f"UNRECOGNISED_{raw}"

    for field in ("mobility_kill", "firepower_kill", "deactivated"):
        spec = block.get(field)
        if spec is not None:                      # absent => this domain has no such bit
            out[field] = bool(appearance_bits >> spec["bit"] & 1)

    pp = block.get("power_plant")
    if pp is not None:
        out["power_plant_on"] = bool(appearance_bits >> pp["bit"] & 1)

    return out
