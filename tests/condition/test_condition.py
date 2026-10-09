"""
Tests for condition.py and the EE (23) / Data (20) paths in dis_ingestor.

PURE UNIT TESTS, NO INFRASTRUCTURE. Byte fixtures for EE and Data are packed
by hand with struct, following the field layout of opendis 1.0's dis7 classes
(ElectromagneticEmissionsPdu, EmissionSystemRecord, EmissionSystemBeamRecord,
EEFundamentalParameterData, DataPdu, FixedDatum). That layout is the only
source: it has NOT been checked against the published IEEE 1278.1 text, the
same caveat the rest of this repo carries. The bytes are then decoded by
opendis's createPdu, an independent decoder of the layout, so the packer and
the decoder agree only if both read the layout the same way.

dis_condition.yaml next to this file is a COPY of the contracts ontology file
(commit f4e59e8), so the tests run without the sibling repo; refresh it when
that file changes.

Run with: py -3 -m pytest tests/condition -q
"""
from __future__ import annotations

import shutil
import struct
import subprocess
import sys
from io import BytesIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from opendis.DataOutputStream import DataOutputStream  # noqa: E402
from opendis.PduFactory import createPdu  # noqa: E402
from opendis.dis7 import DataPdu, ElectromagneticEmissionsPdu, EntityStatePdu  # noqa: E402

import appearance  # noqa: E402
import condition  # noqa: E402
import dis_ingestor  # noqa: E402

HERE = Path(__file__).resolve().parent
TYPE = "1:2:999:9:9:9:0"          # made-up type string, as in the ontology example
BASELINE = "%s: { beams: 4, erp_dbm: 80.0, erp_tolerance_db: 6.0, silence_after_s: 15.0 }\n"
URN = "dis:1:1:10"
SITE = 1

_POWERPLANT = 1 << 21
_DEACTIVATED = 1 << 22
_DAMAGE_SLIGHT = 1 << 3
_DAMAGE_MODERATE = 2 << 3
_DAMAGE_DESTROYED = 3 << 3

_APPEARANCE_TABLE = {
    "populating_sources": {"1": {}},
    "appearance": {
        "1_2": {
            "damage": {"bits": [3, 4], "values": {0: "NONE", 1: "SLIGHT", 2: "MODERATE", 3: "DESTROYED"}},
            "power_plant": {"bit": 21},
            "deactivated": {"bit": 22},
        },
    },
}


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


# ---------------------------------------------------------------------------
# byte fixtures
# ---------------------------------------------------------------------------

def _header(pdu_type: int, family: int, body_len: int) -> bytes:
    return struct.pack(">BBBBIHBB", 7, 1, pdu_type, family, 0, 12 + body_len, 0, 0)


def ee_bytes(site: int, beams_erp: list[float], systems: int = 1) -> bytes:
    """EE with `systems` systems (0 allowed); the beams all go on system one."""
    body = struct.pack(">HHH", site, 1, 10)          # emitting entity id
    body += struct.pack(">HHH", site, 1, 1)          # event id
    body += struct.pack(">BBH", 0, systems, 0)       # state update, n systems, pad
    for i in range(systems):
        erps = beams_erp if i == 0 else []
        beam_len = 4 + 40 + 4 + 4                    # ids + 10 floats + 4 bytes + jamming
        body += struct.pack(">BBH", 5 + len(erps) * (beam_len // 4), len(erps), 0)
        body += struct.pack(">HBB", 100 + i, 1, i)   # emitter name, function, id
        body += struct.pack(">3f", 0.0, 0.0, 0.0)    # location
        for j, erp in enumerate(erps):
            body += struct.pack(">BBH", beam_len // 4, j, 0)
            body += struct.pack(">10f", 3e9, 1e6, erp, 1000.0, 1.0, 0, 0, 0, 0, 0)
            body += struct.pack(">BBBB", 1, 0, 0, 0)
            body += struct.pack(">I", 0)
    return _header(23, 6, len(body)) + body


def data_bytes(site: int, fixed: dict[int, int]) -> bytes:
    body = struct.pack(">HHH", site, 1, 10)          # originating
    body += struct.pack(">HHH", site, 1, 0)          # receiving
    body += struct.pack(">II", 1, 0)                 # request id, padding
    body += struct.pack(">II", len(fixed), 0)        # n fixed, n variable
    for did, val in fixed.items():
        body += struct.pack(">II", did, val)
    return _header(20, 5, len(body)) + body


def es_pdu(bits: int, site: int = SITE, entity: int = 10) -> EntityStatePdu:
    pdu = EntityStatePdu()
    pdu.protocolVersion = 7
    pdu.exerciseID = 1
    pdu.pduType = 1
    pdu.protocolFamily = 1
    pdu.pduStatus = 0
    pdu.capabilities = 0
    pdu.entityAppearance = bits
    pdu.entityID.siteID = site
    pdu.entityID.applicationID = 1
    pdu.entityID.entityID = entity
    t = pdu.entityType
    t.entityKind, t.domain, t.country, t.category, t.subcategory, t.specific, t.extra = 1, 2, 999, 9, 9, 9, 0
    buf = BytesIO()
    pdu.serialize(DataOutputStream(buf))
    return createPdu(buf.getvalue())


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg(tmp_path):
    shutil.copy(HERE / "dis_condition.yaml", tmp_path / "dis_condition.yaml")
    (tmp_path / "dis_condition_baselines.yaml").write_text(BASELINE % f'"{TYPE}"')
    return condition.load_config(str(tmp_path))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def st(cfg, clock):
    return condition.ConditionState(now=clock, config=cfg)


@pytest.fixture(autouse=True)
def _appearance_table():
    old = appearance._TABLE
    appearance._TABLE = _APPEARANCE_TABLE
    yield
    appearance._TABLE = old


def _decode_ee(raw: bytes) -> ElectromagneticEmissionsPdu:
    pdu = createPdu(raw)
    assert isinstance(pdu, ElectromagneticEmissionsPdu)
    return pdu


def _feed_ee(st, raw: bytes, site_id=None):
    st.note_entity_type(URN, TYPE)
    return dis_ingestor._handle_emission(_decode_ee(raw), site_id, None, st)


def _levels(res):
    return {c["source"].removeprefix("CONDITION_SOURCE_"): c["level"].removeprefix("CONDITION_LEVEL_")
            for c in res["claims"]}


def _detail(res, src):
    return next(c["detail"] for c in res["claims"] if c["source"].endswith(src))


# ---------------------------------------------------------------------------
# opendis decodes the hand-packed bytes (no hand decoder needed)
# ---------------------------------------------------------------------------

def test_createpdu_decodes_ee_and_data_bytes():
    ee = _decode_ee(ee_bytes(SITE, [80.0, 79.0]))
    assert dis_ingestor._extract_emission_systems(ee) == [
        {"emitter_name": 100, "beams": [{"erp_dbm": 80.0}, {"erp_dbm": 79.0}]}]
    d = createPdu(data_bytes(SITE, {61000: 92, 7: 1}))
    assert isinstance(d, DataPdu)
    assert [(f.fixedDatumID, f.fixedDatumValue) for f in d.fixedDatumRecords] == [(61000, 92), (7, 1)]


# ---------------------------------------------------------------------------
# emission
# ---------------------------------------------------------------------------

def test_nominal_ee(st):
    _feed_ee(st, ee_bytes(SITE, [80.0] * 4))
    assert _levels(st.resolve(URN, SITE)) == {"EMISSION": "NOMINAL"}


def test_fewer_beams_degraded(st):
    _feed_ee(st, ee_bytes(SITE, [80.0] * 2))
    r = st.resolve(URN, SITE)
    assert _levels(r) == {"EMISSION": "DEGRADED"}
    assert _detail(r, "EMISSION") == "2/4 beams"


def test_low_erp_degraded_and_both(st):
    _feed_ee(st, ee_bytes(SITE, [80.0, 80.0, 80.0, 68.0]))
    r = st.resolve(URN, SITE)
    assert _levels(r) == {"EMISSION": "DEGRADED"}
    assert _detail(r, "EMISSION") == "ERP -12 dB"
    _feed_ee(st, ee_bytes(SITE, [80.0, 60.0]))
    assert _detail(st.resolve(URN, SITE), "EMISSION") == "2/4 beams, ERP -20 dB"


def test_erp_within_tolerance_is_nominal(st):
    _feed_ee(st, ee_bytes(SITE, [80.0, 80.0, 80.0, 74.5]))
    assert _levels(st.resolve(URN, SITE)) == {"EMISSION": "NOMINAL"}


def test_zero_beams_failed_even_unarmed(st):
    _feed_ee(st, ee_bytes(SITE, [], systems=1))
    r = st.resolve(URN, SITE)
    assert _levels(r) == {"EMISSION": "SENSOR_FAILED"}
    assert _detail(r, "EMISSION") == "zero beams"
    _feed_ee(st, ee_bytes(SITE, [], systems=0))
    assert _levels(st.resolve(URN, SITE)) == {"EMISSION": "SENSOR_FAILED"}


def test_armed_then_silent_is_sensor_failed(st, clock):
    _feed_ee(st, ee_bytes(SITE, [80.0] * 4))
    clock.t += 14
    assert _levels(st.resolve(URN, SITE)) == {"EMISSION": "NOMINAL"}
    clock.t += 2
    r = st.resolve(URN, SITE)
    assert _levels(r) == {"EMISSION": "SENSOR_FAILED"}
    assert _detail(r, "EMISSION") == "silent 16s"


def test_silence_explained_by_appearance_power_off(st, clock):
    """Prove-fail target: silence suppression by appearance."""
    _feed_ee(st, ee_bytes(SITE, [80.0] * 4))
    st.note_appearance(URN, SITE, _DAMAGE_SLIGHT, {"damage": "SLIGHT", "power_plant_on": False,
                                                   "deactivated": False}, clock.t)
    clock.t += 60
    r = st.resolve(URN, SITE)
    assert "EMISSION" not in _levels(r)
    assert _levels(r)["APPEARANCE_POWER"] == "NOT_EMITTING"


def test_never_armed_silence_claims_nothing(st, clock):
    clock.t += 600
    assert st.resolve(URN, SITE) is None


def test_unbaselined_type_and_unknown_type(cfg, clock):
    st = condition.ConditionState(now=clock, config=cfg)
    st.note_entity_type(URN, "1:2:1:1:1:1:0")
    assert dis_ingestor._handle_emission(_decode_ee(ee_bytes(SITE, [80.0])), None, None, st)
    assert st.resolve(URN, SITE) is None
    other = condition.ConditionState(now=clock, config=cfg)      # no ES seen yet
    assert dis_ingestor._handle_emission(_decode_ee(ee_bytes(SITE, [80.0])), None, None, other)
    assert other.resolve(URN, SITE) is None


def test_undeclared_site_makes_no_claim(st):
    st.note_entity_type("dis:2:1:10", TYPE)
    raw = ee_bytes(2, [80.0] * 4)
    assert dis_ingestor._handle_emission(_decode_ee(raw), None, None, st)
    assert st.resolve("dis:2:1:10", 2) is None
    st.note_datum("dis:2:1:10", 2, 92)
    assert st.resolve("dis:2:1:10", 2) is None


def test_site_filter_rejects_other_site(st):
    assert not dis_ingestor._handle_emission(_decode_ee(ee_bytes(SITE, [80.0])), 9, None, st)
    d = createPdu(data_bytes(SITE, {61000: 92}))
    assert not dis_ingestor._handle_data(d, 9, None, st)


# ---------------------------------------------------------------------------
# health datum
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,level", [(92, "NOMINAL"), (70, "DEGRADED"), (40, "CRITICAL")])
def test_datum_levels(st, value, level):
    d = createPdu(data_bytes(SITE, {7: 1, 61000: value}))
    assert dis_ingestor._handle_data(d, None, None, st)
    r = st.resolve(URN, SITE)
    assert _levels(r) == {"DATA_HEALTH": level}
    assert _detail(r, "DATA_HEALTH") == f"health {value}"


def test_datum_stale_and_other_id(st, clock):
    dis_ingestor._handle_data(createPdu(data_bytes(SITE, {61000: 40})), None, None, st)
    clock.t += 31
    assert st.resolve(URN, SITE) is None
    dis_ingestor._handle_data(createPdu(data_bytes(SITE, {5: 40})), None, None, st)
    assert st.resolve(URN, SITE) is None


# ---------------------------------------------------------------------------
# worst wins
# ---------------------------------------------------------------------------

def test_worst_wins_datum_beats_slight(st, clock):
    """Prove-fail target: worst-wins ordering."""
    st.note_appearance(URN, SITE, _POWERPLANT | _DAMAGE_SLIGHT,
                       {"damage": "SLIGHT", "power_plant_on": True, "deactivated": False}, clock.t)
    st.note_datum(URN, SITE, 40, clock.t)
    r = st.resolve(URN, SITE)
    assert r["level"] == "CONDITION_LEVEL_CRITICAL"
    assert r["moved_by"] == ["CONDITION_SOURCE_DATA_HEALTH"]


def test_worst_wins_tie_in_source_order(st, clock):
    st.note_appearance(URN, SITE, _POWERPLANT | _DAMAGE_MODERATE,
                       {"damage": "MODERATE", "power_plant_on": True}, clock.t)
    st.note_datum(URN, SITE, 40, clock.t)
    r = st.resolve(URN, SITE)
    assert r["level"] == "CONDITION_LEVEL_CRITICAL"
    assert r["moved_by"] == ["CONDITION_SOURCE_APPEARANCE_DAMAGE", "CONDITION_SOURCE_DATA_HEALTH"]
    assert [c["source"].removeprefix("CONDITION_SOURCE_") for c in r["claims"]] == [
        "APPEARANCE_DAMAGE", "APPEARANCE_POWER", "DATA_HEALTH"]


def test_destroyed_beats_everything(st, clock):
    _feed_ee(st, ee_bytes(SITE, []))
    st.note_datum(URN, SITE, 40, clock.t)
    st.note_appearance(URN, SITE, _DAMAGE_DESTROYED,
                       {"damage": "DESTROYED", "power_plant_on": False}, clock.t)
    r = st.resolve(URN, SITE)
    assert r["level"] == "CONDITION_LEVEL_DESTROYED"
    assert r["moved_by"] == ["CONDITION_SOURCE_APPEARANCE_DAMAGE"]


def test_no_claims_is_none(st):
    assert st.resolve(URN, SITE) is None


def test_unrecognised_damage_makes_no_claim(st, clock):
    st.note_appearance(URN, SITE, _POWERPLANT, {"damage": "UNRECOGNISED_7", "power_plant_on": True}, clock.t)
    assert _levels(st.resolve(URN, SITE)) == {"APPEARANCE_POWER": "NOMINAL"}


def test_empty_decoded_drops_old_appearance_claims(st, clock):
    st.note_appearance(URN, SITE, _DAMAGE_DESTROYED, {"damage": "DESTROYED"}, clock.t)
    st.note_appearance(URN, SITE, 0, {}, clock.t)
    assert st.resolve(URN, SITE) is None


def test_missing_ontology_claims_nothing(tmp_path, clock):
    cfg = condition.load_config(str(tmp_path))
    assert cfg == {}
    st = condition.ConditionState(now=clock, config=cfg)
    st.note_datum(URN, SITE, 40)
    st.note_appearance(URN, SITE, 8, {"damage": "SLIGHT"})
    assert st.resolve(URN, SITE) is None


# ---------------------------------------------------------------------------
# zero after claim
# ---------------------------------------------------------------------------

def test_zero_after_claim_armed(st, clock):
    bits = _POWERPLANT
    st.note_appearance(URN, SITE, bits, {"damage": "NONE", "power_plant_on": True}, clock.t)
    assert st.appearance_zero_is_claim(URN, SITE, 0)
    assert not st.appearance_zero_is_claim(URN, SITE, bits)
    d = appearance.decode(0, 1, 2, SITE, zero_is_claim=True)
    assert d == {"damage": "NONE", "power_plant_on": False, "deactivated": False}
    st.note_appearance(URN, SITE, 0, d, clock.t)
    assert _levels(st.resolve(URN, SITE)) == {"APPEARANCE_DAMAGE": "NOMINAL",
                                              "APPEARANCE_POWER": "NOT_EMITTING"}


def test_zero_without_arming_is_no_claim(st):
    assert not st.appearance_zero_is_claim(URN, SITE, 0)
    assert appearance.decode(0, 1, 2, SITE) == {}


def test_zero_site_not_opted_in(st, clock):
    st.note_appearance("dis:2:1:10", 2, _POWERPLANT, {"power_plant_on": True}, clock.t)
    assert not st.appearance_zero_is_claim("dis:2:1:10", 2, 0)


def test_zero_is_claim_still_needs_declaration():
    assert appearance.decode(0, 1, 2, 9, zero_is_claim=True) == {}


# ---------------------------------------------------------------------------
# proto shape
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def condition_cls(tmp_path_factory):
    pytest.importorskip("grpc_tools", reason="grpc_tools missing: cannot generate contracts gencode")
    proto_dir = ROOT.parent / "openddil-contracts" / "proto"
    proto = proto_dir / "openddil" / "telemetry" / "v1" / "telemetry.proto"
    common = proto_dir / "openddil" / "common" / "v1" / "quantity.proto"
    if not proto.exists():
        pytest.skip("contracts proto dir not found")
    out = tmp_path_factory.mktemp("gen")
    r = subprocess.run([sys.executable, "-m", "grpc_tools.protoc", f"-I{proto_dir}",
                        f"--python_out={out}", str(proto), str(common)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"protoc failed: {r.stderr[:200]}")
    sys.path.insert(0, str(out))
    try:
        from openddil.telemetry.v1 import telemetry_pb2  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"gencode not importable: {exc}")
    return telemetry_pb2.Condition


def test_resolved_dict_parses_into_condition_proto(st, clock, condition_cls):
    from google.protobuf import json_format  # noqa: PLC0415
    _feed_ee(st, ee_bytes(SITE, [80.0] * 2))
    st.note_datum(URN, SITE, 40, clock.t)
    r = st.resolve(URN, SITE)
    msg = json_format.ParseDict(r, condition_cls())
    assert len(msg.claims) == 2 and len(msg.moved_by) == 1


# ---------------------------------------------------------------------------
# end to end through the run-loop helpers
# ---------------------------------------------------------------------------

def test_es_after_ee_carries_condition_and_bare_es_does_not(st, clock):
    first = dis_ingestor._build_entity_state_record(es_pdu(0), 100, st, clock.t)
    assert "condition" not in first
    assert _feed_ee(st, ee_bytes(SITE, [80.0] * 2))
    rec = dis_ingestor._build_entity_state_record(es_pdu(0), 100, st, clock.t)
    assert rec["condition"]["level"] == "CONDITION_LEVEL_DEGRADED"
    assert rec["condition"]["moved_by"] == ["CONDITION_SOURCE_EMISSION"]
    assert rec["appearance"] == {}                 # zero guard stands: unarmed


def test_es_zero_after_claim_flows_to_appearance_and_condition(st, clock):
    rec = dis_ingestor._build_entity_state_record(es_pdu(_POWERPLANT), 100, st, clock.t)
    assert rec["appearance"]["power_plant_on"] is True
    rec = dis_ingestor._build_entity_state_record(es_pdu(0), 100, st, clock.t)
    assert rec["appearance"]["power_plant_on"] is False
    assert rec["condition"]["level"] == "CONDITION_LEVEL_NOT_EMITTING"
