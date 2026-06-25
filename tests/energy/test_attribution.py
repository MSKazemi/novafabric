"""Slice A1/A4 — attribute energy to a capsule's actions (ADR-0093).

On a host with no per-action energy counters (the common case — n1, GB10 SoC),
attribution produces honest ``unavailable`` receipts joined to each action, and
records what the node *could* read so an auditor can tell "hardware cannot" from
"NovaFabric failed". The receipts round-trip through ``energy-receipts.jsonl``.
"""

from __future__ import annotations

import json
from pathlib import Path

from novafabric.capsule.ulid_util import new_ulid
from novafabric.energy import Confidence, MeasurementSource
from novafabric.energy._attribution import attest_capsule, load_receipts, write_receipts


def _make_capsule(tmp: Path) -> tuple[Path, str, list[str]]:
    run_id = new_ulid()
    (tmp / "capsule.yaml").write_text(f"run_id: {run_id}\n")
    mc1, mc2, tc1 = new_ulid(), new_ulid(), new_ulid()
    (tmp / "model-calls.jsonl").write_text(
        json.dumps({"model_call_id": mc1, "model": "gpt-4"})
        + "\n"
        + json.dumps({"model_call_id": mc2, "model": "gpt-4"})
        + "\n"
    )
    (tmp / "tool-calls.jsonl").write_text(
        json.dumps({"tool_call_id": tc1, "name": "search"}) + "\n"
    )
    return tmp, run_id, [mc1, mc2, tc1]


def test_attest_capsule_produces_unavailable_receipts_per_action(tmp_path):
    capsule, run_id, action_ids = _make_capsule(tmp_path)
    receipts = attest_capsule(capsule, rapl_base=tmp_path / "no-rapl", node_id="n1")

    assert len(receipts) == 3
    assert {r.action_ref.id for r in receipts} == set(action_ids)
    for r in receipts:
        assert r.confidence is Confidence.UNKNOWN
        assert r.measurement_source is MeasurementSource.UNAVAILABLE
        assert r.measured_joules is None
        assert r.run_id == run_id
        assert r.consistency_errors() == []


def test_write_and_load_receipts_roundtrip(tmp_path):
    capsule, _, _ = _make_capsule(tmp_path)
    receipts = attest_capsule(capsule, rapl_base=tmp_path / "no-rapl", node_id="n1")

    path = write_receipts(capsule, receipts)
    assert path == capsule / "energy-receipts.jsonl"
    assert path.exists()

    loaded = load_receipts(capsule)
    assert len(loaded) == 3
    assert {r.receipt_id for r in loaded} == {r.receipt_id for r in receipts}


def test_attest_capsule_tolerates_malformed_input(tmp_path):
    # no capsule.yaml (run_id falls back to a generated ULID), a malformed line,
    # and a record with no id (action id falls back to a generated ULID)
    (tmp_path / "model-calls.jsonl").write_text(
        "not json\n"
        + json.dumps({"model": "gpt-4"})  # no id field
        + "\n"
    )
    receipts = attest_capsule(tmp_path, rapl_base=tmp_path / "no-rapl", node_id="n1")
    assert len(receipts) == 1
    assert len(receipts[0].action_ref.id) == 26  # generated ULID
    assert receipts[0].consistency_errors() == []


def test_attest_capsule_empty_when_no_action_streams(tmp_path):
    (tmp_path / "capsule.yaml").write_text(f"run_id: {new_ulid()}\n")
    receipts = attest_capsule(tmp_path, rapl_base=tmp_path / "no-rapl", node_id="n1")
    assert receipts == []
