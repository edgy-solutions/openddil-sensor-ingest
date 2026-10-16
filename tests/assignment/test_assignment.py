"""
Unit tests for assign.py (assignment rules, nation stamping) and for the
dis_ingestor wiring around them.

PURE UNIT TESTS, NO INFRASTRUCTURE. Every id, edge, nation and coordinate is
synthetic: ids dis:1:1:<n>, edges edge-a / edge-b, nations ATL / BDR, and a
made-up lat/lon box. The headline test feeds one stream of Entity State
observations to two AssignmentFilters built from the SAME rules JSON, one per
edge, and checks they partition the stream with no coordination -- the
property the whole design rests on (first-match determinism).

Run with: python -m pytest -q tests/assignment
"""
from __future__ import annotations

import json
import logging
import math
import sys
from io import BytesIO
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from opendis.DataOutputStream import DataOutputStream  # noqa: E402
from opendis.PduFactory import createPdu  # noqa: E402
from opendis.dis7 import (  # noqa: E402
    EntityID,
    EntityStatePdu,
    EntityType,
    EventIdentifier,
    FirePdu,
    MunitionDescriptor,
    SimulationAddress,
    Vector3Double,
)

import assign  # noqa: E402
import condition  # noqa: E402
import dis_ingestor  # noqa: E402

A_AXIS = 6378137.0
F = 1 / 298.257223563
E2 = F * (2 - F)


def geodetic_to_ecef(lat_deg: float, lon_deg: float, h: float = 0.0):
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    n = A_AXIS / math.sqrt(1 - E2 * math.sin(lat) ** 2)
    return ((n + h) * math.cos(lat) * math.cos(lon),
            (n + h) * math.cos(lat) * math.sin(lon),
            (n * (1 - E2) + h) * math.sin(lat))


# Made-up box: lat 10..20, lon 30..40.
BOX = {"south": 10, "west": 30, "north": 20, "east": 40}
IN_BOX = (15.0, 35.0)
OUT_BOX = (-30.0, -60.0)


def _rules() -> list[dict]:
    return [
        {"name": "pinned", "edge": "edge-a",
         "match": {"ids": ["dis:1:1:1", "dis:1:1:9"]}},
        {"name": "box", "edge": "edge-a", "match": {"geofence": BOX}},
        {"name": "mark", "edge": "edge-b", "match": {"marking": "ZZ-.*"}},
        {"name": "cty", "edge": "edge-a", "match": {"country": [77]}},
    ]


RULES_JSON = json.dumps(_rules())


def _mk(this_edge: str):
    return assign.parse_assignment_rules(RULES_JSON, this_edge, "edge-b")


# (n, kind, marking, country, (lat, lon), expected owner)
STREAM = [
    (1, 1, "ZZ-1", 5, OUT_BOX, "edge-a"),    # ids rule beats the marking rule
    (2, 1, "ZZ-2", 5, IN_BOX, "edge-a"),     # box is listed before marking
    (3, 1, "ZZ-3", 5, OUT_BOX, "edge-b"),    # marking
    (4, 1, "AA", 77, OUT_BOX, "edge-a"),     # country
    (5, 1, "AA", 5, OUT_BOX, "edge-b"),      # nothing matches: fallback
    (6, 1, "AA", 77, IN_BOX, "edge-a"),      # box before country
    (7, 2, "AA", 5, IN_BOX, "edge-b"),       # kind 2 in the box: fallback
    (8, 2, "ZZ-8", 77, OUT_BOX, "edge-b"),   # kind 2 skips marking/country
    (9, 2, "AA", 5, OUT_BOX, "edge-a"),      # kind 2 but named by an ids rule
    (10, 1, "ZZ-10", 77, OUT_BOX, "edge-b"),  # marking is before country
    (11, 1, "AA", 5, (10.0001, 30.0001), "edge-a"),  # just inside the corner
    (12, 1, "AA", 5, (15.0, 41.0), "edge-b"),  # just outside the box
]


def _feed(f):
    for n, kind, marking, country, (lat, lon), _owner in STREAM:
        f.observe_entity_state(f"dis:1:1:{n}", kind, marking, country,
                               geodetic_to_ecef(lat, lon))


# ---------------------------------------------------------------------------
# Parsing: fails closed, naming the rule index and the field
# ---------------------------------------------------------------------------
def _parse(rules, fallback="edge-b", this_edge="edge-a"):
    raw = rules if isinstance(rules, str) else json.dumps(rules)
    return assign.parse_assignment_rules(raw, this_edge, fallback)


def _rule(match, name="r", edge="edge-a"):
    return {"name": name, "edge": edge, "match": match}


def test_empty_or_unset_is_none():
    assert assign.parse_assignment_rules("", "edge-a", "edge-b") is None
    assert assign.parse_assignment_rules("   ", "edge-a", "") is None


@pytest.mark.parametrize("rules, where", [
    ("not json", "rules"),
    ({"a": 1}, "rules"),
    ([["x"]], "rules[0]"),
    ([_rule({"ids": ["dis:1:1:1"]}), _rule({"bogus": 1})], "rules[1].match"),
    ([_rule({"ids": ["dis:1:1:1"], "country": [1]})], "rules[0].match"),
    ([_rule({})], "rules[0].match"),
    ([_rule({"ids": ["dis:1:1"]})], "rules[0].match.ids"),
    ([_rule({"ids": ["dis:1:1:1"]}), _rule({"ids": ["dis:1:1:1"]})],
     "rules[1].match.ids"),
    ([_rule({"ids": ["dis:1:1:2", "dis:1:1:2"]})], "rules[0].match.ids"),
    ([_rule({"marking": "(unclosed"})], "rules[0].match.marking"),
    ([_rule({"geofence": {**BOX, "north": 91}})], "rules[0].match.geofence"),
    ([_rule({"geofence": {**BOX, "south": -91}})], "rules[0].match.geofence"),
    ([_rule({"geofence": {**BOX, "east": 181}})], "rules[0].match.geofence"),
    ([_rule({"geofence": {**BOX, "west": -181}})], "rules[0].match.geofence"),
    ([_rule({"geofence": {**BOX, "south": 25}})], "rules[0].match.geofence"),
    ([_rule({"geofence": {"south": 1, "west": 2, "north": 3}})],
     "rules[0].match.geofence"),
    ([_rule({"country": [70000]})], "rules[0].match.country"),
    ([_rule({"country": [-1]})], "rules[0].match.country"),
    ([_rule({"country": ["x"]})], "rules[0].match.country"),
    ([_rule({"country": [True]})], "rules[0].match.country"),
    ([_rule({"country": []})], "rules[0].match.country"),
    ([_rule({"country": [1]}, name="")], "rules[0].name"),
    ([_rule({"country": [1]}, edge="")], "rules[0].edge"),
])
def test_parse_failures_name_rule_and_field(rules, where):
    with pytest.raises(ValueError) as exc:
        _parse(rules)
    assert where in str(exc.value)


def test_fallback_edge_required_when_rules_set():
    with pytest.raises(ValueError) as exc:
        _parse([_rule({"country": [1]})], fallback="")
    assert "fallback" in str(exc.value)


def test_good_rules_parse_and_len_and_log_line(caplog):
    f = _mk("edge-a")
    assert len(f) == 4
    with caplog.at_level("INFO"):
        f.log_summary(logging.getLogger("t"))
    assert ("Assignment rules: 4 (this edge edge-a, fallback edge-b) -- "
            "pinned, box, mark, cty") in caplog.text


# ---------------------------------------------------------------------------
# Geodesy
# ---------------------------------------------------------------------------
def test_ecef_equator_prime_meridian():
    lat, lon = assign.ecef_to_geodetic(A_AXIS, 0.0, 0.0)
    assert abs(lat) < 1e-6 and abs(lon) < 1e-6


def test_ecef_north_pole():
    b = A_AXIS * (1 - F)
    lat, _lon = assign.ecef_to_geodetic(0.0, 0.0, b)
    assert abs(lat - 90.0) < 1e-6


@pytest.mark.parametrize("lat, lon, h", [
    (45.0, 100.0, 0.0), (-33.3, -120.5, 1500.0), (15.0, 35.0, 10000.0),
    (-80.0, 179.9, 0.0),
])
def test_ecef_midlatitude_roundtrip(lat, lon, h):
    got = assign.ecef_to_geodetic(*geodetic_to_ecef(lat, lon, h))
    assert abs(got[0] - lat) < 1e-6 and abs(got[1] - lon) < 1e-6


def test_geofence_across_antimeridian():
    rules = [_rule({"geofence": {"south": -10, "west": 170, "north": 10,
                                 "east": -170}})]
    f = _parse(rules)
    f.observe_entity_state("dis:1:1:1", 1, "", 0, geodetic_to_ecef(0, 175))
    f.observe_entity_state("dis:1:1:2", 1, "", 0, geodetic_to_ecef(0, -175))
    f.observe_entity_state("dis:1:1:3", 1, "", 0, geodetic_to_ecef(0, 0))
    assert f.owner("dis:1:1:1") == "edge-a"
    assert f.owner("dis:1:1:2") == "edge-a"
    assert f.owner("dis:1:1:3") == "edge-b"


# ---------------------------------------------------------------------------
# The two-sidecar partition
# ---------------------------------------------------------------------------
def test_two_sidecars_partition_the_stream():
    fa, fb = _mk("edge-a"), _mk("edge-b")
    _feed(fa)
    _feed(fb)
    urns = {f"dis:1:1:{n}" for n, *_ in STREAM}
    adm_a = {u for u in urns if u in fa}
    adm_b = {u for u in urns if u in fb}
    assert adm_a & adm_b == set()                      # (i) disjoint
    assert adm_a | adm_b == urns                       # (ii) complete
    for n, _k, _m, _c, _p, owner in STREAM:            # (iii) first match
        u = f"dis:1:1:{n}"
        assert (u in fa) == (owner == "edge-a"), u
        assert (u in fb) == (owner == "edge-b"), u
    # (iv) kind 2 inside the geofence goes to the fallback, not the box edge
    assert "dis:1:1:7" in fb and "dis:1:1:7" not in fa


def test_decision_is_sticky():
    f = _mk("edge-b")
    f.observe_entity_state("dis:1:1:50", 1, "AA", 5, geodetic_to_ecef(*OUT_BOX))
    assert f.owner("dis:1:1:50") == "edge-b"
    got = f.observe_entity_state("dis:1:1:50", 1, "AA", 5,
                                 geodetic_to_ecef(*IN_BOX))
    assert got == "edge-b" and f.owner("dis:1:1:50") == "edge-b"
    assert "dis:1:1:50" in f


def test_unresolved_is_not_admitted_anywhere():
    fa, fb = _mk("edge-a"), _mk("edge-b")
    for f in (fa, fb):
        assert "dis:1:1:77" not in f
        assert f.drop_reason("dis:1:1:77") == "entity_unresolved"
        assert f.owner("dis:1:1:77") is None


def test_ids_rule_admits_before_any_entity_state():
    fa, fb = _mk("edge-a"), _mk("edge-b")
    assert "dis:1:1:1" in fa
    assert "dis:1:1:1" not in fb
    assert fb.drop_reason("dis:1:1:1") == "entity_assigned_elsewhere"


def test_drop_reason_assigned_elsewhere_after_es():
    fa = _mk("edge-a")
    fa.observe_entity_state("dis:1:1:5", 1, "AA", 5, geodetic_to_ecef(*OUT_BOX))
    assert "dis:1:1:5" not in fa
    assert fa.drop_reason("dis:1:1:5") == "entity_assigned_elsewhere"


# ---------------------------------------------------------------------------
# Nation stamping
# ---------------------------------------------------------------------------
NATION_JSON = json.dumps({
    "overrides": {"dis:1:1:1": "BDR", "dis:1:1:2": "BDR"},
    "rules": [
        {"name": "box", "nation": "ATL", "match": {"geofence": BOX}},
        {"name": "cty", "nation": "BDR", "match": {"country": [77]}},
    ],
})


def _stamper():
    return assign.parse_nation_rules(NATION_JSON)


def test_nation_unset_is_none():
    assert assign.parse_nation_rules("") is None


def _nr(**kw):
    base = {"name": "r", "nation": "ATL", "match": {"country": [1]}}
    base.update(kw)
    return json.dumps({"rules": [base]})


@pytest.mark.parametrize("raw", [
    "nope",
    json.dumps([]),
    json.dumps({"overrides": {"dis:1:1": "ATL"}}),
    json.dumps({"overrides": {"dis:1:1:1": "atl"}}),
    json.dumps({"overrides": {"dis:1:1:1": "ATLX"}}),
    _nr(nation="AT"),
    _nr(match={"ids": ["dis:1:1:1"]}),
    _nr(name=""),
    _nr(match={"marking": "("}),
    _nr(match={}),
    json.dumps({"extra": 1}),
])
def test_nation_parse_failures(raw):
    with pytest.raises(ValueError):
        assign.parse_nation_rules(raw)


def test_nation_override_beats_rule():
    s = _stamper()
    # dis:1:1:1 sits in the ATL box but is overridden to BDR
    assert s.observe_entity_state("dis:1:1:1", 1, "AA", 5,
                                  geodetic_to_ecef(*IN_BOX)) == "BDR"


def test_nation_first_match_wins():
    s = _stamper()
    assert s.observe_entity_state("dis:1:1:3", 1, "AA", 77,
                                  geodetic_to_ecef(*IN_BOX)) == "ATL"


def test_nation_kind2_only_by_override():
    s = _stamper()
    assert s.observe_entity_state("dis:1:1:4", 2, "AA", 77,
                                  geodetic_to_ecef(*IN_BOX)) is None
    assert s.observe_entity_state("dis:1:1:2", 2, "AA", 77,
                                  geodetic_to_ecef(*OUT_BOX)) == "BDR"


def test_nation_sticky_including_none():
    s = _stamper()
    assert s.observe_entity_state("dis:1:1:6", 1, "AA", 5,
                                  geodetic_to_ecef(*OUT_BOX)) is None
    assert s.observe_entity_state("dis:1:1:6", 1, "AA", 77,
                                  geodetic_to_ecef(*IN_BOX)) is None
    assert s.nation_for("dis:1:1:6") is None


def test_nation_for_serves_non_es_records():
    s = _stamper()
    assert s.nation_for("dis:1:1:1") == "BDR"       # override, before any ES
    assert s.nation_for("dis:1:1:9") is None        # unknown
    s.observe_entity_state("dis:1:1:3", 1, "AA", 77, geodetic_to_ecef(*IN_BOX))
    assert s.nation_for("dis:1:1:3") == "ATL"


def _roundtrip(pdu):
    buf = BytesIO()
    pdu.serialize(DataOutputStream(buf))
    return createPdu(buf.getvalue())


def _es_pdu(site, app, ent, kind=1, country=0, marking="", ecef=(0.0, 0.0, 0.0)):
    pdu = EntityStatePdu()
    pdu.protocolVersion = 7
    pdu.exerciseID = 1
    pdu.pduType = 1
    pdu.protocolFamily = 1
    pdu.pduStatus = 0
    pdu.entityAppearance = 0
    pdu.capabilities = 0
    pdu.entityID.siteID = site
    pdu.entityID.applicationID = app
    pdu.entityID.entityID = ent
    pdu.entityType.entityKind = kind
    pdu.entityType.country = country
    chars = list(marking.encode("ascii"))[:11]
    pdu.marking.characterSet = 1
    pdu.marking.characters = chars + [0] * (11 - len(chars))
    pdu.entityLocation.x, pdu.entityLocation.y, pdu.entityLocation.z = ecef
    return _roundtrip(pdu)


def _fire_pdu(firing):
    pdu = FirePdu(
        munitionExpendableID=EntityID(siteID=0, applicationID=0, entityID=0),
        eventID=EventIdentifier(
            simulationAddress=SimulationAddress(site=firing[0], application=firing[1]),
            eventNumber=1),
        location=Vector3Double(x=1.0, y=2.0, z=3.0),
        descriptor=MunitionDescriptor(
            munitionType=EntityType(entityKind=2, domain=9, country=225,
                                    category=2, subcategory=1, specific=1, extra=0),
            warhead=0, fuse=0, quantity=1),
        range_=0.0)
    pdu.firingEntityID = EntityID(siteID=firing[0], applicationID=firing[1],
                                  entityID=firing[2])
    pdu.targetEntityID = EntityID(siteID=0, applicationID=0, entityID=0)
    pdu.protocolVersion = 7
    pdu.exerciseID = 1
    pdu.pduType = 2
    pdu.protocolFamily = 2
    pdu.pduStatus = 0
    return _roundtrip(pdu)


def test_es_record_carries_stamped_nation():
    s = _stamper()
    pdu = _es_pdu(1, 1, 3, kind=1, country=77, ecef=geodetic_to_ecef(*IN_BOX))
    rec = dis_ingestor._build_entity_state_record(pdu, 144, condition.ConditionState())
    dis_ingestor._stamp_nation(rec, s)
    assert rec["origin_node"]["stamped_nation"] == "ATL"


def test_es_record_has_no_key_when_none():
    s = _stamper()
    pdu = _es_pdu(1, 1, 6, kind=1, country=5, ecef=geodetic_to_ecef(*OUT_BOX))
    rec = dis_ingestor._build_entity_state_record(pdu, 144, condition.ConditionState())
    dis_ingestor._stamp_nation(rec, s)
    assert "stamped_nation" not in rec["origin_node"]
    assert "stamped_nation" not in json.dumps(rec)


def test_fire_record_stamped_on_launcher():
    s = _stamper()
    rec = dis_ingestor._extract_fire(_fire_pdu((1, 1, 1)))
    dis_ingestor._stamp_nation(rec, s)
    assert rec["provenance"]["stamped_nation"] == "BDR"    # override on launcher
    rec = dis_ingestor._extract_fire(_fire_pdu((1, 1, 8)))
    dis_ingestor._stamp_nation(rec, s)
    assert "stamped_nation" not in rec["provenance"]


def test_resupply_not_stamped():
    s = _stamper()
    rec = {"pdu_type": "resupply_received", "launcher_urn": "dis:1:1:1",
           "provenance": {"edge_id": "e", "region_id": "r"}}
    dis_ingestor._stamp_nation(rec, s)
    assert "stamped_nation" not in rec["provenance"]


# ---------------------------------------------------------------------------
# dis_ingestor wiring
# ---------------------------------------------------------------------------
def _count(reason):
    return dis_ingestor.DIS_PDUS_FILTERED.labels(reason=reason)._value.get()


def test_build_filter_rules_plus_admit_list_exits():
    with mock.patch.object(dis_ingestor, "OPENDDIL_EDGE_ID", "edge-a"):
        with pytest.raises(SystemExit) as exc:
            dis_ingestor._build_entity_filter("dis:1:1:1", "", RULES_JSON, "edge-b")
        assert exc.value.code == 1
        with pytest.raises(SystemExit) as exc:
            dis_ingestor._build_entity_filter("", "dis:1:1:1", RULES_JSON, "edge-b")
        assert exc.value.code == 1


def test_build_filter_bad_rules_exit_names_error(caplog):
    with mock.patch.object(dis_ingestor, "OPENDDIL_EDGE_ID", "edge-a"):
        with pytest.raises(SystemExit) as exc:
            dis_ingestor._build_entity_filter("", "", '[{"name": "x"}]', "edge-b")
    assert exc.value.code == 1
    assert "rules[0]" in caplog.text


def test_build_filter_returns_assignment_filter():
    with mock.patch.object(dis_ingestor, "OPENDDIL_EDGE_ID", "edge-a"):
        got = dis_ingestor._build_entity_filter("", "", RULES_JSON, "edge-b")
    assert isinstance(got, assign.AssignmentFilter)
    assert dis_ingestor._build_entity_filter("", "", "", "") is None


def test_accept_entity_state_admits_here_drops_elsewhere():
    fa, fb = _mk("edge-a"), _mk("edge-b")
    pdu = _es_pdu(1, 1, 4, kind=1, country=77, marking="AA",
                  ecef=geodetic_to_ecef(*OUT_BOX))      # owner edge-a (country)
    before = _count("entity_assigned_elsewhere")
    assert dis_ingestor._accept_entity_state(pdu, 1, fa) is True
    assert _count("entity_assigned_elsewhere") == before
    assert dis_ingestor._accept_entity_state(pdu, 1, fb) is False
    assert _count("entity_assigned_elsewhere") - before == 1


def test_accept_other_pdu_unresolved_counted():
    fa = _mk("edge-a")
    before = _count("entity_unresolved")
    assert dis_ingestor._accept_effector(_fire_pdu((1, 1, 99)), 1, fa) is False
    assert _count("entity_unresolved") - before == 1


def test_drop_reason_signature_and_other_filters_unchanged():
    assert dis_ingestor._entity_drop_reason(frozenset(), "dis:1:1:1") == "entity"
    assert dis_ingestor._entity_drop_reason(
        dis_ingestor.DenyFilter(set()), "dis:1:1:1") == "entity_denied"


# ---------------------------------------------------------------------------
# Non-asset sources by application, any site
# ---------------------------------------------------------------------------
def _with_sources(raw):
    return mock.patch.object(dis_ingestor, "DIS_NON_ASSET_SOURCES",
                             dis_ingestor._parse_non_asset_sources(raw))


def test_any_site_source_drops_every_site():
    before = _count("non_asset_source")
    with _with_sources("*:900"):
        assert not dis_ingestor._accept_entity_state(_es_pdu(1, 900, 1), None, None)
        assert not dis_ingestor._accept_entity_state(_es_pdu(2, 900, 1), None, None)
        assert dis_ingestor._accept_entity_state(_es_pdu(1, 901, 1), None, None)
    assert _count("non_asset_source") - before == 2


def test_any_site_kind_source_drops_only_that_kind():
    with _with_sources("*:900:2"):
        assert not dis_ingestor._accept_entity_state(_es_pdu(1, 900, 1, kind=2), None, None)
        assert not dis_ingestor._accept_entity_state(_es_pdu(2, 900, 1, kind=2), None, None)
        assert dis_ingestor._accept_entity_state(_es_pdu(1, 900, 2, kind=1), None, None)


def test_any_site_source_applies_to_resupply():
    with _with_sources("*:900"):
        assert not dis_ingestor._accept_resupply_received(
            {"launcher_urn": "dis:7:900:1"}, None, None)


def test_any_site_duplicate_fatal_but_coexists_with_site_form():
    with pytest.raises(SystemExit):
        dis_ingestor._parse_non_asset_sources("*:5 *:5")
    got = dis_ingestor._parse_non_asset_sources("*:5 1:5")
    assert len(got) == 2
    assert dis_ingestor._format_source((None, 5, 2)) == "*:5:2"
    assert dis_ingestor._format_source((1, 5, None)) == "1:5"


# ---------------------------------------------------------------------------
# Nation stamping: the "exempt" override
# ---------------------------------------------------------------------------
EXEMPT_JSON = json.dumps({
    "overrides": {"dis:1:1:7": "exempt", "dis:1:1:1": "BDR"},
    "rules": [{"name": "box", "nation": "ATL", "match": {"geofence": BOX}}],
})


def _exempt_stamper():
    return assign.parse_nation_rules(EXEMPT_JSON)


def test_nation_exempt_parses():
    assert assign.parse_nation_rules(
        json.dumps({"overrides": {"dis:1:1:7": "exempt"}})) is not None


@pytest.mark.parametrize("bad", ["EXEMPT", "Exempt", "", None])
def test_nation_exempt_spellings_rejected(bad):
    with pytest.raises(ValueError, match='or be "exempt"'):
        assign.parse_nation_rules(json.dumps({"overrides": {"dis:1:1:7": bad}}))


def test_nation_exempt_entity_matching_a_rule_stays_unstamped():
    s = _exempt_stamper()
    assert s.observe_entity_state("dis:1:1:7", 1, "AA", 5,
                                  geodetic_to_ecef(*IN_BOX)) is None
    assert s.nation_for("dis:1:1:7") is None


def test_nation_exempt_nation_for_before_any_es():
    assert _exempt_stamper().nation_for("dis:1:1:7") is None


def test_nation_exempt_is_sticky():
    s = _exempt_stamper()
    assert s.observe_entity_state("dis:1:1:7", 1, "AA", 5,
                                  geodetic_to_ecef(*OUT_BOX)) is None
    assert s.observe_entity_state("dis:1:1:7", 1, "AA", 5,
                                  geodetic_to_ecef(*IN_BOX)) is None


def test_nation_non_exempt_entity_matching_the_same_rule_is_stamped():
    s = _exempt_stamper()
    assert s.observe_entity_state("dis:1:1:8", 1, "AA", 5,
                                  geodetic_to_ecef(*IN_BOX)) == "ATL"


def test_nation_exempt_records_carry_no_stamped_nation():
    s = _exempt_stamper()
    pdu = _es_pdu(1, 1, 7, kind=1, country=5, ecef=geodetic_to_ecef(*IN_BOX))
    rec = dis_ingestor._build_entity_state_record(pdu, 144, condition.ConditionState())
    dis_ingestor._stamp_nation(rec, s)
    assert "stamped_nation" not in rec["origin_node"]
    er = {"pdu_type": "event_report", "entity_id_urn": "dis:1:1:7",
          "provenance": {"edge_id": "e", "region_id": "r"}}
    dis_ingestor._stamp_nation(er, s)
    assert er["provenance"] == {"edge_id": "e", "region_id": "r"}


def test_nation_log_line_counts_exempt(caplog):
    with caplog.at_level("INFO"):
        _exempt_stamper().log_summary(logging.getLogger("t"))
    assert "overrides: 2 (1 exempt)" in caplog.text
    assert "dis:1:1:7" in caplog.text
