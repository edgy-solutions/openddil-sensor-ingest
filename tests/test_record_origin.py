"""Every record shape the sidecar publishes names the tier that produced it.

Entity State carries it under `origin_node {edge_id, region_id}`; Remove, Fire,
Detonation, Resupply Received and Event Report under `provenance {edge_id,
region_id}`. Each record here is built through the real extract function from a
PDU made by the existing decode tests' own builders, with the configured tier
patched to distinct values so a record that reads a stale or default value fails.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_TESTS.parent))
for _sub in ("event_report", "effector", "remove_entity", "assignment"):
    sys.path.insert(0, str(_TESTS / _sub))

from opendis.PduFactory import createPdu  # noqa: E402

import dis_ingestor  # noqa: E402
import test_assignment as asg  # noqa: E402
import test_effector_decode as eff  # noqa: E402
import test_event_report_decode as ev  # noqa: E402
import test_remove_entity_decode as rem  # noqa: E402
import test_resupply_received_decode as rsp  # noqa: E402

TIER = {"edge_id": "edge-x", "region_id": "region-y"}


@pytest.fixture(autouse=True)
def _tier(monkeypatch):
    monkeypatch.setattr(dis_ingestor, "OPENDDIL_EDGE_ID", TIER["edge_id"])
    monkeypatch.setattr(dis_ingestor, "OPENDDIL_REGION_ID", TIER["region_id"])


def _remove():
    data = rem._remove_entity_bytes(originating=(1, 1, 0), receiving=(1, 1, 5), request_id=1)
    return dis_ingestor._extract_remove_entity(createPdu(data))


def _fire():
    pdu = eff._build_fire_pdu(firing=(1, 1, 7), target=(1, 1, 8), event=(1, 1, 3))
    return dis_ingestor._extract_fire(createPdu(eff._serialize(pdu)))


def _detonation():
    pdu = eff._build_detonation_pdu(
        firing=(1, 1, 7), target=(1, 1, 8), event=(1, 1, 1),
        detonation_result=3, quantity=1, warhead=5, fuse=6)
    return dis_ingestor._extract_detonation(createPdu(eff._serialize(pdu)))


def _resupply():
    data = rsp._build_opendis((1, 2, 3), (1, 3, 4), [(rsp.TYPE_A, 1.0)])
    return dis_ingestor._parse_resupply_received(data)


def _event_report():
    pdu = ev._build_event_report_pdu(originating=(1, 1, 5), receiving=(0, 0, 0), event_type=1)
    return dis_ingestor._extract_event_report(createPdu(ev._serialize(pdu)))


def test_entity_state_names_its_tier():
    rec = dis_ingestor._extract_entity_state(asg._es_pdu(1, 1, 1), 0)
    assert {k: rec["origin_node"][k] for k in ("edge_id", "region_id")} == TIER


@pytest.mark.parametrize("build", [_remove, _fire, _detonation, _resupply, _event_report],
                         ids=lambda f: f.__name__.strip("_"))
def test_record_names_its_tier(build):
    rec = build()
    assert {k: rec["provenance"][k] for k in ("edge_id", "region_id")} == TIER
