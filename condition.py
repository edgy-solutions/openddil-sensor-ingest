"""
DIS condition claims -- resolve one condition per entity from several sources.

An entity's condition can be claimed by three independent DIS sources: the
Entity State appearance bits, Electromagnetic Emission (EE) PDUs, and a Data
PDU health datum. Each source keeps its own claim and the worst wins, so a
consumer can say what moved an asset. The result rides on the Entity State
record (as `condition`) rather than on a record of its own: the downstream
latest-state topic is compacted per entity, so a separate EE-only or Data-only
record would overwrite the asset's latest state.

Like appearance.py this module is the WIRE-SIDE half only. The INTERPRETATION
-- the order that decides "worst", the appearance-to-level mapping, the
per-type emission baselines, the health thresholds -- lives in the ontology
(`dis_condition.yaml`, plus the deployment's `dis_condition_baselines.yaml`)
because it is curated by PR (ADR-0016). Nothing is hardcoded here except the
shape of the answer.

THREE REFUSALS, each the same one appearance.py makes:

  1. A source that is not declared under its `populating_sources` makes no
     claim. Silence is never read as health (ADR-0026 amendment, clause 3).
  2. No claim is manufactured from absence. An entity that has never sent a
     beam is not "silent", it is unknown: silence only counts once the entity
     has been seen emitting (ARMED), because the work side may never send EE
     at all. An entity type with no declared baseline gets no emission claim
     and is counted, because comparing against a made-up nominal would be the
     same invented health.
  3. Silence that is explained claims nothing. An emitter that stops because
     its power plant is off, it is deactivated or it is destroyed is not a
     failed sensor; the appearance claim already says why.

STATE IS IN-MEMORY PER SIDECAR. A restart forgets arming and the appearance
zero-after-claim state; both re-arm on the next EE / non-zero appearance.
That is declared, not hidden: the failure direction is a withheld claim.

The returned dict is the proto JSON shape of openddil.telemetry.v1.Condition
(enum names in full), so the mapping can copy it without interpreting it.
"""
from __future__ import annotations

import datetime
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from prometheus_client import Counter

import appearance as appearance_table

logger = logging.getLogger("dis_ingestor.condition")

_ONTOLOGY_DIR = os.getenv("ONTOLOGY_DIR", "/ontology")
_CONFIG: dict[str, Any] | None = None

CONDITION_CLAIMS = Counter(
    "dis_condition_claims_total",
    "Condition claims made by a source, by source and level (no entity label: "
    "cardinality stays bounded)",
    ["source", "level"],
)
CONDITION_UNBASELINED = Counter(
    "dis_condition_unbaselined_total",
    "Emission PDUs read with no declared baseline for the entity type (or no "
    "Entity State seen yet to learn the type from): no emission claim made",
)
CONDITION_UNRECOGNISED = Counter(
    "dis_condition_unrecognised_total",
    "Appearance damage values the ontology does not map: no claim made",
)


def _read_yaml(path: Path) -> dict[str, Any] | None:
    try:
        import yaml  # noqa: PLC0415
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return None


def load_config(ontology_dir: str | None = None) -> dict[str, Any]:
    """Read dis_condition.yaml and merge the deployment baselines over it.

    Never raises. A missing or unreadable dis_condition.yaml yields {} and
    the module then claims nothing, which is the same honest outcome as an
    undeclared source. The baselines file is optional: the shipped map is
    empty on purpose because baselines are per deployment.
    """
    base = Path(ontology_dir or _ONTOLOGY_DIR)
    path = base / "dis_condition.yaml"
    try:
        cfg = _read_yaml(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load %s (%s) -- condition not resolved", path, exc)
        return {}
    if cfg is None:
        logger.info("No dis_condition.yaml at %s -- condition not resolved", path)
        return {}

    try:
        extra = _read_yaml(base / "dis_condition_baselines.yaml")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not load dis_condition_baselines.yaml (%s) -- "
                       "using the baselines in dis_condition.yaml only", exc)
        extra = None
    if extra:
        emission = dict(cfg.get("emission") or {})
        merged = dict(emission.get("baselines") or {})
        merged.update({str(k): v for k, v in extra.items()})
        emission["baselines"] = merged
        cfg = {**cfg, "emission": emission}
    return cfg


def _config() -> dict[str, Any]:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = load_config()
    return _CONFIG


def _rfc3339(t: float) -> str:
    dt = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _Entity:
    __slots__ = ("appearance_claims", "appearance_armed", "emission_armed",
                 "last_ee", "last_ee_beams", "emission_claim", "silence_after_s",
                 "datum_value", "datum_time", "entity_type")

    def __init__(self) -> None:
        self.appearance_claims: list[tuple[str, str, str]] = []  # source, level, detail
        self.appearance_armed = False
        self.emission_armed = False
        self.last_ee: float | None = None
        self.last_ee_beams: bool | None = None                    # last EE had >= 1 beam
        self.emission_claim: tuple[str, str] | None = None        # level, detail
        self.silence_after_s: float | None = None
        self.datum_value: float | None = None
        self.datum_time: float | None = None
        self.entity_type: str | None = None


class ConditionState:
    """Per-entity condition state, keyed by entity_id_urn.

    `now` is injectable so tests drive silence and staleness without sleeping.
    Every method also takes an explicit `now` for the same reason; omitted, it
    reads the clock.
    """

    def __init__(self, now: Callable[[], float] = time.time,
                 config: dict[str, Any] | None = None) -> None:
        self._clock = now
        self._cfg_override = config
        self._entities: dict[str, _Entity] = {}
        self._appearance_times: dict[str, float] = {}

    @property
    def cfg(self) -> dict[str, Any]:
        return self._cfg_override if self._cfg_override is not None else _config()

    def _ent(self, urn: str) -> _Entity:
        e = self._entities.get(urn)
        if e is None:
            e = self._entities[urn] = _Entity()
        return e

    def _t(self, now: float | None) -> float:
        return self._clock() if now is None else now

    # -- entity type, learned from Entity State --------------------------
    def note_entity_type(self, urn: str, type_str: str) -> None:
        self._ent(urn).entity_type = type_str

    def entity_type(self, urn: str) -> str | None:
        e = self._entities.get(urn)
        return e.entity_type if e else None

    # -- appearance ------------------------------------------------------
    def appearance_zero_is_claim(self, urn: str, site: int, bits: int) -> bool:
        """True iff an all-zero field is, for this entity, a claim.

        Opt-in per populating source in dis_condition.yaml, and only after the
        entity has sent a non-zero appearance (armed). The generator that is
        opted in sets the power-plant bit whenever it claims, so after its
        first claim a zero can only mean "plant off, undamaged". Unarmed, or
        a site that did not opt in, the decoder's zero guard stands.
        """
        if bits != 0:
            return False
        opted = (self.cfg.get("appearance") or {}).get("zero_after_claim") or {}
        if not opted.get(str(site)):
            return False
        e = self._entities.get(urn)
        return bool(e and e.appearance_armed)

    def note_appearance(self, urn: str, site: int, bits: int,
                        decoded: dict[str, Any], now: float | None = None) -> None:
        t = self._t(now)
        e = self._ent(urn)
        if bits != 0:
            e.appearance_armed = True
        claims: list[tuple[str, str, str]] = []
        # Empty decoded: the ES said nothing (undeclared source, zero guard,
        # unmapped kind/domain). Keep no claims from before -- a stale
        # "destroyed" must not outlive an ES that no longer says it.
        amap = self.cfg.get("appearance") or {}
        if "damage" in decoded:
            name = decoded["damage"]
            level = (amap.get("damage") or {}).get(name)
            if level is None:
                CONDITION_UNRECOGNISED.inc()   # said something we cannot read
            else:
                claims.append(("APPEARANCE_DAMAGE", level, f"damage {name}"))
        if "deactivated" in decoded or "power_plant_on" in decoded:
            if decoded.get("deactivated") is True and amap.get("deactivated"):
                claims.append(("APPEARANCE_POWER", amap["deactivated"], "deactivated"))
            elif decoded.get("power_plant_on") is False and amap.get("power_plant_off"):
                claims.append(("APPEARANCE_POWER", amap["power_plant_off"],
                               "power plant off"))
            elif "power_plant_on" in decoded or decoded.get("deactivated") is False:
                claims.append(("APPEARANCE_POWER", "NOMINAL", "power plant on"))
        e.appearance_claims = claims
        self._appearance_times[urn] = t
        for src, lvl, _ in claims:
            CONDITION_CLAIMS.labels(source=src, level=lvl).inc()

    # -- emission --------------------------------------------------------
    def note_emission(self, urn: str, site: int, entity_type_str: str | None,
                      systems: list[dict[str, Any]], now: float | None = None) -> None:
        emission = self.cfg.get("emission") or {}
        if str(site) not in (emission.get("populating_sources") or {}):
            return
        t = self._t(now)
        base = (emission.get("baselines") or {}).get(entity_type_str) if entity_type_str else None
        if not base:
            CONDITION_UNBASELINED.inc()
            return
        e = self._ent(urn)
        e.last_ee = t
        e.silence_after_s = float(base.get("silence_after_s", 0) or 0)
        beams = [b for s in systems for b in (s.get("beams") or [])]
        e.last_ee_beams = bool(beams)
        if not beams:
            # An explicit EE with nothing in it is the emitter's positive,
            # deliberate statement that it is not radiating: NOT_EMITTING, not
            # a failure, and it needs no arming to be believed. It DOES arm
            # silence, though: the emitter has spoken, so a later total absence
            # of EEs past silence_after_s must still read SENSOR_FAILED.
            e.emission_armed = True
            e.emission_claim = ("NOT_EMITTING", "zero beams")
        else:
            e.emission_armed = True
            parts: list[str] = []
            want = int(base.get("beams", 0))
            if len(beams) < want:
                parts.append(f"{len(beams)}/{want} beams")
            floor = float(base["erp_dbm"]) - float(base.get("erp_tolerance_db", 0))
            low = [float(b["erp_dbm"]) for b in beams if float(b["erp_dbm"]) < floor]
            if low:
                # Reported against the baseline ERP, not the tolerance floor:
                # "-12 dB" reads as 12 dB below nominal.
                parts.append(f"ERP -{round(float(base['erp_dbm']) - min(low))} dB")
            e.emission_claim = (("DEGRADED", ", ".join(parts)) if parts
                                else ("NOMINAL", f"{len(beams)} beams"))
        CONDITION_CLAIMS.labels(source="EMISSION", level=e.emission_claim[0]).inc()

    def emitting(self, urn: str, now: float | None = None) -> bool | None:
        """Is the entity radiating? True / False, or None for "no claim".

        True: the last EE (seen within the baseline's silence_after_s) had at
        least one beam. False: it had zero beams -- the emitter's own statement
        that it is not radiating. None: no EE seen, no baseline for the type or
        an undeclared source, silence_after_s missing or <= 0, or the last EE
        is silence_after_s old or older. Silence is never a signal.
        """
        e = self._entities.get(urn)
        if e is None or e.last_ee is None or e.last_ee_beams is None:
            return None
        if not e.silence_after_s or e.silence_after_s <= 0:
            return None
        if self._t(now) - e.last_ee >= e.silence_after_s:
            return None
        return e.last_ee_beams

    # -- health datum ----------------------------------------------------
    def _datum_level(self, value: float) -> str:
        dh = self.cfg.get("data_health") or {}
        if value < dh.get("critical_below", float("-inf")):
            return "CRITICAL"
        if value < dh.get("degraded_below", float("-inf")):
            return "DEGRADED"
        return "NOMINAL"

    def note_datum(self, urn: str, site: int, value: float, now: float | None = None) -> None:
        dh = self.cfg.get("data_health") or {}
        if str(site) not in (dh.get("populating_sources") or {}):
            return
        e = self._ent(urn)
        e.datum_value = value
        e.datum_time = self._t(now)
        CONDITION_CLAIMS.labels(source="DATA_HEALTH", level=self._datum_level(value)).inc()

    # -- resolution ------------------------------------------------------
    def resolve(self, urn: str, site: int, now: float | None = None) -> dict[str, Any] | None:
        cfg = self.cfg
        e = self._entities.get(urn)
        if not cfg or e is None:
            return None
        t = self._t(now)
        claims: list[dict[str, str]] = []

        def add(source: str, level: str, detail: str, observed: float) -> None:
            claims.append({"source": f"CONDITION_SOURCE_{source}",
                           "level": f"CONDITION_LEVEL_{level}",
                           "detail": detail, "observed_at": _rfc3339(observed)})

        seen = self._appearance_times.get(urn, t)
        for src, lvl, detail in e.appearance_claims:
            add(src, lvl, detail, seen)

        explained = {lvl for _, lvl, _ in e.appearance_claims} & {
            "NOT_EMITTING", "DEACTIVATED", "DESTROYED"}
        if (e.emission_armed and e.last_ee is not None and e.silence_after_s
                and t - e.last_ee >= e.silence_after_s):
            if not explained:
                add("EMISSION", "SENSOR_FAILED", f"silent {round(t - e.last_ee)}s", e.last_ee)
        elif e.emission_claim is not None and e.last_ee is not None:
            add("EMISSION", e.emission_claim[0], e.emission_claim[1], e.last_ee)

        dh = cfg.get("data_health") or {}
        if (e.datum_value is not None and e.datum_time is not None
                and t - e.datum_time <= dh.get("max_age_s", 0)):
            add("DATA_HEALTH", self._datum_level(e.datum_value),
                f"health {e.datum_value:g}", e.datum_time)

        if not claims:
            return None
        order = list(cfg.get("level_order") or [])
        sorder = list(cfg.get("source_order") or [])

        def lvl_rank(c: dict[str, str]) -> int:
            n = c["level"].removeprefix("CONDITION_LEVEL_")
            return order.index(n) if n in order else len(order)

        def src_rank(c: dict[str, str]) -> int:
            n = c["source"].removeprefix("CONDITION_SOURCE_")
            return sorder.index(n) if n in sorder else len(sorder)

        claims.sort(key=src_rank)
        worst = min(lvl_rank(c) for c in claims)
        top = [c for c in claims if lvl_rank(c) == worst]
        return {"level": top[0]["level"],
                "moved_by": [c["source"] for c in top],
                "claims": claims}
