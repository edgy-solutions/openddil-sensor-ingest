"""Entity assignment rules and nation stamping for the DIS sidecar.

PURE: no sockets, no Kafka, no prometheus. dis_ingestor.py owns the wiring
and the counters; this module owns the decisions, so they are unit-testable
with nothing but tuples.

WHY ONE SHARED, ORDERED RULE LIST
---------------------------------
Several sidecars can sit on one multicast group, each seeing the whole
stream. They must split it with no coordination between them. Every sidecar
is therefore handed the SAME rule list (the same JSON), and each asks one
question of every entity: "which edge does the first matching rule name?" A
sidecar admits the entity iff that edge is its own. Because first-match over
a fixed list is deterministic, the admitted sets are disjoint and, with a
declared fallback edge owning whatever no rule matched, complete. Nothing
is exchanged between sidecars; the property follows from the data alone.

WHY DECISIONS ARE STICKY
------------------------
The decision for a URN is made once, from the first Entity State that
decides it, and kept for the process lifetime. A moving asset must not
change edges mid-run: assignment is static, and a geofence is only a way of
saying where an asset STARTS. A restart re-decides from the next Entity
State, and changes only for a geofence asset that has since moved.

WHY NO SIDECAR CLAIMS AN UNDECIDED URN
--------------------------------------
Before its first Entity State a URN has no marking, country or position, so
no rule except an `ids` rule can place it. The fallback edge does not get it
by default either: a later Entity State could place it elsewhere, and two
sidecars would then both have published it. A non-ES PDU for an undecided
URN is dropped (reason `entity_unresolved`) and the next Entity State
decides it.

WHY KIND 1 ONLY FOR GEOFENCE, MARKING AND COUNTRY
-------------------------------------------------
Those three describe a platform. A munition's position, marking and country
say where the round is, not who owns it; matching them would hand a flying
round to whichever edge it overflies. A non-platform entity is placed by an
`ids` rule or else owned by the fallback edge. (A munition is still also
admitted at its launcher's edge by dis_ingestor's claim path, unchanged.)
"""
from __future__ import annotations

import json
import math
import re

_URN_RE = re.compile(r"^dis:(\d+):(\d+):(\d+)$")
_NATION_RE = re.compile(r"^[A-Z]{3}$")
# An override value that keeps an entity out of every stamping rule. A literal
# string, not JSON null: Helm drops null-valued keys when it merges values files.
NATION_EXEMPT = "exempt"

# WGS84
_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_B = _A * (1 - _F)
_EP2 = (_A * _A - _B * _B) / (_B * _B)


def ecef_to_geodetic(x: float, y: float, z: float) -> tuple[float, float]:
    """WGS84 ECEF metres -> (latitude, longitude) in degrees.

    Bowring's method, iterated until the latitude moves less than 1e-12 rad
    (a few passes), which is far inside the 1e-6 degree the geofence needs.
    """
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    if p < 1e-9:                      # on the polar axis
        return (90.0 if z >= 0 else -90.0), math.degrees(lon)
    theta = math.atan2(z * _A, p * _B)
    lat = math.atan2(z + _EP2 * _B * math.sin(theta) ** 3,
                     p - _E2 * _A * math.cos(theta) ** 3)
    for _ in range(8):
        n = _A / math.sqrt(1 - _E2 * math.sin(lat) ** 2)
        new = math.atan2(z + _E2 * n * math.sin(lat), p)
        done = abs(new - lat) < 1e-12
        lat = new
        if done:
            break
    return math.degrees(lat), math.degrees(lon)


# ---------------------------------------------------------------------------
# Match parsing (shared by assignment rules and nation rules)
# ---------------------------------------------------------------------------
def _num(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _parse_match(match: object, where: str, allow_ids: bool,
                 seen_ids: set[str]) -> tuple[str, object]:
    """One `match` object -> (kind, compiled payload). Raises ValueError
    naming `where` (the rule's index) and the offending field."""
    w = f"{where}.match"
    if not isinstance(match, dict) or len(match) != 1:
        raise ValueError(f"{w}: expected an object with exactly one key")
    (key, val), = match.items()
    allowed = ("ids", "geofence", "marking", "country") if allow_ids \
        else ("geofence", "marking", "country")
    if key not in allowed:
        raise ValueError(f"{w}: unknown match key {key!r} (expected one of "
                         f"{', '.join(allowed)})")
    if key == "ids":
        if not isinstance(val, list) or not val:
            raise ValueError(f"{w}.ids: expected a non-empty list of "
                             "dis:<site>:<app>:<entity>")
        for urn in val:
            if not isinstance(urn, str) or not _URN_RE.match(urn):
                raise ValueError(f"{w}.ids: malformed entity id {urn!r}")
            if urn in seen_ids:
                raise ValueError(f"{w}.ids: entity id {urn!r} listed twice")
            seen_ids.add(urn)
        return "ids", tuple(val)
    if key == "geofence":
        names = ("south", "west", "north", "east")
        if not isinstance(val, dict) or set(val) != set(names):
            raise ValueError(f"{w}.geofence: expected exactly "
                             "south, west, north, east")
        if not all(_num(val[k]) for k in names):
            raise ValueError(f"{w}.geofence: values must be numbers")
        s, wst, n, e = (float(val[k]) for k in names)
        if not (-90 <= s <= 90 and -90 <= n <= 90):
            raise ValueError(f"{w}.geofence: latitude outside +-90")
        if not (-180 <= wst <= 180 and -180 <= e <= 180):
            raise ValueError(f"{w}.geofence: longitude outside +-180")
        if s > n:
            raise ValueError(f"{w}.geofence: south is greater than north")
        return "geofence", (s, wst, n, e)
    if key == "marking":
        if not isinstance(val, str):
            raise ValueError(f"{w}.marking: expected a regex string")
        try:
            return "marking", re.compile(val)
        except re.error as exc:
            raise ValueError(f"{w}.marking: bad regex {val!r}: {exc}") from None
    if (not isinstance(val, list) or not val
            or not all(isinstance(c, int) and not isinstance(c, bool)
                       and 0 <= c <= 65535 for c in val)):
        raise ValueError(f"{w}.country: expected a non-empty list of "
                         "ints 0..65535")
    return "country", frozenset(val)


def _in_box(box: tuple[float, float, float, float], lat: float, lon: float) -> bool:
    south, west, north, east = box
    if not (south <= lat <= north):
        return False
    if west <= east:
        return west <= lon <= east
    return lon >= west or lon <= east          # crosses the antimeridian


class _Facts:
    """The facts an Entity State supplies, with the geodetic position
    computed only if a geofence rule is actually evaluated."""

    __slots__ = ("marking", "country", "ecef", "_geo")

    def __init__(self, marking, country, ecef) -> None:  # noqa: ANN001
        self.marking = marking or ""
        self.country = country
        self.ecef = ecef
        self._geo = None

    def geo(self) -> tuple[float, float]:
        if self._geo is None:
            self._geo = ecef_to_geodetic(*self.ecef)
        return self._geo


def _facts_match(kind: str, data: object, f: _Facts) -> bool:
    if kind == "geofence":
        return _in_box(data, *f.geo())          # type: ignore[arg-type]
    if kind == "marking":
        return data.fullmatch(f.marking) is not None   # type: ignore[union-attr]
    return f.country in data                    # type: ignore[operator]


def _top_list(raw: str, what: str) -> object:
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"{what}: invalid JSON: {exc}") from None


def _name(rule: dict, where: str) -> str:
    n = rule.get("name")
    if not isinstance(n, str) or not n.strip():
        raise ValueError(f"{where}.name: expected a non-empty string")
    return n


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------
def parse_assignment_rules(raw_json: str, this_edge: str,
                           fallback_edge: str) -> "AssignmentFilter | None":
    """DIS_ASSIGNMENT_RULES -> AssignmentFilter, or None when unset.

    Fails closed: any defect raises ValueError naming the rule index and the
    field, and the caller refuses to start. A filter that is wrong either
    admits nothing (the sidecar looks dead) or admits what another sidecar
    also admits (the split this exists to enforce never happens).
    """
    if raw_json is None or not raw_json.strip():
        return None
    rules = _top_list(raw_json, "rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("rules: expected a non-empty JSON list")
    if not isinstance(fallback_edge, str) or not fallback_edge.strip():
        raise ValueError("fallback edge (DIS_FALLBACK_EDGE) is required "
                         "when rules are set")
    if not isinstance(this_edge, str) or not this_edge.strip():
        raise ValueError("this edge (OPENDDIL_EDGE_ID) is required "
                         "when rules are set")
    seen: set[str] = set()
    parsed = []
    for i, rule in enumerate(rules):
        where = f"rules[{i}]"
        if not isinstance(rule, dict):
            raise ValueError(f"{where}: expected an object")
        extra = set(rule) - {"name", "edge", "match"}
        if extra:
            raise ValueError(f"{where}: unknown key {sorted(extra)[0]!r}")
        name = _name(rule, where)
        edge = rule.get("edge")
        if not isinstance(edge, str) or not edge.strip():
            raise ValueError(f"{where}.edge: expected a non-empty string")
        kind, data = _parse_match(rule.get("match"), where, True, seen)
        parsed.append((name, edge, kind, data))
    return AssignmentFilter(parsed, this_edge, fallback_edge)


class AssignmentFilter:
    """Drop-in for the admit set at every call site: `urn in f` means
    "admitted here". See the module note for the rules of the game."""

    def __init__(self, rules, this_edge: str, fallback_edge: str) -> None:  # noqa: ANN001
        self._rules = tuple(rules)                 # (name, edge, kind, data)
        self.this_edge = this_edge
        self.fallback_edge = fallback_edge
        self._ids = {urn: edge for (_n, edge, kind, data) in self._rules
                     if kind == "ids" for urn in data}
        self._others = tuple(r for r in self._rules if r[2] != "ids")
        self._owner: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._rules)

    @property
    def names(self) -> list[str]:
        return [r[0] for r in self._rules]

    def log_summary(self, logger) -> None:  # noqa: ANN001
        logger.info("Assignment rules: %d (this edge %s, fallback %s) -- %s",
                    len(self), self.this_edge, self.fallback_edge,
                    ", ".join(self.names))

    def _decide_by_ids(self, urn: str) -> str | None:
        edge = self._ids.get(urn)
        if edge is not None:
            self._owner[urn] = edge
        return edge

    def owner(self, urn: str) -> str | None:
        got = self._owner.get(urn)
        return got if got is not None else self._decide_by_ids(urn)

    def observe_entity_state(self, urn: str, kind: int, marking: str,
                             country: int, ecef) -> str:  # noqa: ANN001
        """Decide `urn` once and return its owner edge."""
        got = self.owner(urn)
        if got is not None:
            return got
        owner = self.fallback_edge
        if kind == 1:
            facts = _Facts(marking, country, ecef)
            for _name_, edge, rkind, data in self._others:
                if _facts_match(rkind, data, facts):
                    owner = edge
                    break
        self._owner[urn] = owner
        return owner

    def __contains__(self, urn: object) -> bool:
        return self.owner(urn) == self.this_edge    # type: ignore[arg-type]

    def drop_reason(self, urn: str) -> str:
        return ("entity_unresolved" if self.owner(urn) is None
                else "entity_assigned_elsewhere")


# ---------------------------------------------------------------------------
# Nation stamping
# ---------------------------------------------------------------------------
def parse_nation_rules(raw_json: str) -> "NationStamper | None":
    """DIS_NATION_RULES -> NationStamper, or None when unset. Fails closed
    like parse_assignment_rules. `ids` are not a match kind here: a named
    entity belongs in `overrides`."""
    if raw_json is None or not raw_json.strip():
        return None
    doc = _top_list(raw_json, "nation rules")
    if not isinstance(doc, dict):
        raise ValueError("nation rules: expected a JSON object")
    extra = set(doc) - {"overrides", "rules"}
    if extra:
        raise ValueError(f"nation rules: unknown key {sorted(extra)[0]!r}")
    overrides = doc.get("overrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("overrides: expected an object of urn -> nation")
    for urn, nation in overrides.items():
        if not _URN_RE.match(urn):
            raise ValueError(f"overrides: malformed entity id {urn!r}")
        if not isinstance(nation, str) or not (
                nation == NATION_EXEMPT or _NATION_RE.match(nation)):
            raise ValueError(
                f"overrides[{urn}]: nation must match [A-Z]{{3}} or be "
                f"\"{NATION_EXEMPT}\"")
    rules = doc.get("rules", [])
    if not isinstance(rules, list):
        raise ValueError("rules: expected a list")
    parsed = []
    for i, rule in enumerate(rules):
        where = f"rules[{i}]"
        if not isinstance(rule, dict):
            raise ValueError(f"{where}: expected an object")
        extra = set(rule) - {"name", "nation", "match"}
        if extra:
            raise ValueError(f"{where}: unknown key {sorted(extra)[0]!r}")
        name = _name(rule, where)
        nation = rule.get("nation")
        if not isinstance(nation, str) or not _NATION_RE.match(nation):
            raise ValueError(f"{where}.nation: must match [A-Z]{{3}}")
        kind, data = _parse_match(rule.get("match"), where, False, set())
        parsed.append((name, nation, kind, data))
    # exempt is held as None: "this urn is decided, and has no nation".
    return NationStamper(
        {u: (None if n == NATION_EXEMPT else n) for u, n in overrides.items()},
        parsed)


class NationStamper:
    """Which nation, if any, an entity's records are stamped with.

    Overrides first (any kind), then, for kind 1 only, the rules first-match.
    An exempt override (held as None) decides too: the rules are not consulted.
    Sticky per URN, a None included: an entity no rule matched stays
    unstamped rather than flipping when it later drives into a box.
    """

    def __init__(self, overrides: dict, rules) -> None:  # noqa: ANN001
        self._overrides = overrides
        self._rules = tuple(rules)                 # (name, nation, kind, data)
        self._decided: dict[str, str | None] = {}

    def observe_entity_state(self, urn: str, kind: int, marking: str,
                             country: int, ecef) -> str | None:  # noqa: ANN001
        if urn in self._decided:
            return self._decided[urn]
        nation = None
        if urn in self._overrides:
            nation = self._overrides[urn]          # exempt is None, and final
        elif kind == 1:
            facts = _Facts(marking, country, ecef)
            for _name_, n, rkind, data in self._rules:
                if _facts_match(rkind, data, facts):
                    nation = n
                    break
        self._decided[urn] = nation
        return nation

    def log_summary(self, logger) -> None:  # noqa: ANN001
        exempt = sorted(u for u, n in self._overrides.items() if n is None)
        logger.info("Nation rules: %d, overrides: %d (%d exempt)%s",
                    len(self._rules), len(self._overrides), len(exempt),
                    (" -- exempt: " + ", ".join(exempt)) if exempt else "")

    def nation_for(self, urn: str) -> str | None:
        """For records that are not an Entity State (Fire, Remove, ...).
        An override applies even before any ES; otherwise whatever the ES
        decided, or None."""
        if urn in self._decided:
            return self._decided[urn]
        return self._overrides.get(urn)
