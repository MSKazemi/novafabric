"""Slice A1/A4 — apportioned receipts from RAPL samples (ADR-0093 §A1, priority 4).

When ``energy-samples.jsonl`` is present, attribution integrates the node joules
over the run window (wraparound-safe via ``rapl_delta_joules``) and apportions to
each action by *time-share* — each action's wall-clock fraction of the total.
The result is graded ``confidence="apportioned"``, ``attribution_method="time_share"``,
honestly — never ``measured``. With no samples, attribution falls back to the
existing honest ``unavailable`` path (covered by ``test_attribution.py``).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from novafabric.capsule.ulid_util import new_ulid
from novafabric.energy import AttributionMethod, Confidence, MeasurementSource
from novafabric.energy._attribution import attest_capsule, attest_capsule_with_samples


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _make_capsule_with_samples(
    tmp: Path,
    *,
    start_uj: int,
    end_uj: int,
    durations_ms: tuple[float, float],
) -> tuple[Path, str, list[str], float]:
    run_id = new_ulid()
    (tmp / "capsule.yaml").write_text(f"run_id: {run_id}\n")
    mc1, mc2 = new_ulid(), new_ulid()
    d1, d2 = durations_ms
    _write_jsonl(
        tmp / "model-calls.jsonl",
        [
            {"model_call_id": mc1, "model": "gpt-4", "duration_ms": d1},
            {"model_call_id": mc2, "model": "gpt-4", "duration_ms": d2},
        ],
    )
    # two RAPL snapshots bracketing the run window
    _write_jsonl(
        tmp / "energy-samples.jsonl",
        [
            {"ts": 1000.0, "domains_uj": {"intel-rapl:0": start_uj}, "gap": False},
            {"ts": 1002.0, "domains_uj": {"intel-rapl:0": end_uj}, "gap": False},
        ],
    )
    node_joules = (end_uj - start_uj) / 1_000_000
    return tmp, run_id, [mc1, mc2], node_joules


def test_apportioned_receipts_sum_to_integrated_node_total(tmp_path):
    capsule, run_id, ids, node_joules = _make_capsule_with_samples(
        tmp_path, start_uj=1_000_000, end_uj=4_000_000, durations_ms=(300.0, 100.0)
    )

    receipts = attest_capsule_with_samples(capsule, node_id="n1")

    assert len(receipts) == 2
    assert {r.action_ref.id for r in receipts} == set(ids)

    for r in receipts:
        assert r.confidence is Confidence.APPORTIONED
        assert r.measurement_source is MeasurementSource.RAPL_PKG
        assert r.attribution_method is AttributionMethod.TIME_SHARE
        assert r.run_id == run_id
        assert r.measured_joules is not None
        assert r.attribution_basis  # non-empty explanation
        # forgery guard must pass: a counter must be named
        assert r.consistency_errors() == []

    total = sum(r.measured_joules for r in receipts if r.measured_joules is not None)
    assert math.isclose(total, node_joules, rel_tol=1e-9)

    # time-share: 300/400 vs 100/400 of 3.0 J
    by_id = {r.action_ref.id: r.measured_joules for r in receipts}
    assert math.isclose(by_id[ids[0]], 3.0 * 0.75, rel_tol=1e-9)
    assert math.isclose(by_id[ids[1]], 3.0 * 0.25, rel_tol=1e-9)


def test_equal_split_when_no_durations_present(tmp_path):
    capsule, _, ids, node_joules = _make_capsule_with_samples(
        tmp_path, start_uj=0, end_uj=2_000_000, durations_ms=(0.0, 0.0)
    )
    # rewrite model-calls without any duration/timestamp fields → even split
    _write_jsonl(
        capsule / "model-calls.jsonl",
        [{"model_call_id": ids[0]}, {"model_call_id": ids[1]}],
    )
    receipts = attest_capsule_with_samples(capsule, node_id="n1")
    assert len(receipts) == 2
    for r in receipts:
        assert r.measured_joules is not None
        assert math.isclose(r.measured_joules, node_joules / 2, rel_tol=1e-9)
        assert r.consistency_errors() == []


def test_started_finished_iso_fields_drive_time_share(tmp_path):
    run_id = new_ulid()
    (tmp_path / "capsule.yaml").write_text(f"run_id: {run_id}\n")
    a, b = new_ulid(), new_ulid()
    _write_jsonl(
        tmp_path / "tool-calls.jsonl",
        [
            {
                "tool_call_id": a,
                "started": "2026-06-19T00:00:00+00:00",
                "finished": "2026-06-19T00:00:03+00:00",
            },
            {
                "tool_call_id": b,
                "started": "2026-06-19T00:00:00+00:00",
                "finished": "2026-06-19T00:00:01+00:00",
            },
        ],
    )
    _write_jsonl(
        tmp_path / "energy-samples.jsonl",
        [
            {"ts": 1.0, "domains_uj": {"intel-rapl:0": 0}, "gap": False},
            {"ts": 2.0, "domains_uj": {"intel-rapl:0": 4_000_000}, "gap": False},
        ],
    )
    receipts = attest_capsule_with_samples(tmp_path, node_id="n1")
    by_id = {r.action_ref.id: r.measured_joules for r in receipts}
    # 3s vs 1s of 4.0 J → 3.0 / 1.0
    assert math.isclose(by_id[a], 3.0, rel_tol=1e-9)
    assert math.isclose(by_id[b], 1.0, rel_tol=1e-9)


def test_falls_back_to_unavailable_when_no_samples(tmp_path):
    run_id = new_ulid()
    (tmp_path / "capsule.yaml").write_text(f"run_id: {run_id}\n")
    mc = new_ulid()
    _write_jsonl(tmp_path / "model-calls.jsonl", [{"model_call_id": mc}])
    # no energy-samples.jsonl present
    receipts = attest_capsule_with_samples(
        tmp_path, rapl_base=tmp_path / "no-rapl", node_id="n1"
    )
    assert len(receipts) == 1
    assert receipts[0].confidence is Confidence.UNKNOWN
    assert receipts[0].measurement_source is MeasurementSource.UNAVAILABLE
    assert receipts[0].measured_joules is None
    assert receipts[0].consistency_errors() == []


def test_all_gap_samples_fall_back_to_unavailable(tmp_path):
    run_id = new_ulid()
    (tmp_path / "capsule.yaml").write_text(f"run_id: {run_id}\n")
    mc = new_ulid()
    _write_jsonl(tmp_path / "model-calls.jsonl", [{"model_call_id": mc}])
    # samples exist but are all gap markers → no usable node total
    _write_jsonl(
        tmp_path / "energy-samples.jsonl",
        [
            {"ts": 1.0, "domains_uj": {}, "gap": True},
            {"ts": 2.0, "domains_uj": {}, "gap": True},
        ],
    )
    receipts = attest_capsule_with_samples(
        tmp_path, rapl_base=tmp_path / "no-rapl", node_id="n1"
    )
    assert len(receipts) == 1
    assert receipts[0].confidence is Confidence.UNKNOWN
    assert receipts[0].measurement_source is MeasurementSource.UNAVAILABLE


def test_duration_s_field_drives_time_share(tmp_path):
    run_id = new_ulid()
    (tmp_path / "capsule.yaml").write_text(f"run_id: {run_id}\n")
    a, b = new_ulid(), new_ulid()
    _write_jsonl(
        tmp_path / "tool-calls.jsonl",
        [
            {"tool_call_id": a, "duration_s": 3.0},
            {"tool_call_id": b, "duration_s": 1.0},
        ],
    )
    _write_jsonl(
        tmp_path / "energy-samples.jsonl",
        [
            {"ts": 1.0, "domains_uj": {"intel-rapl:0": 0}, "gap": False},
            {"ts": 2.0, "domains_uj": {"intel-rapl:0": 4_000_000}, "gap": False},
        ],
    )
    receipts = attest_capsule_with_samples(tmp_path, node_id="n1")
    by_id = {r.action_ref.id: r.measured_joules for r in receipts}
    assert math.isclose(by_id[a], 3.0, rel_tol=1e-9)
    assert math.isclose(by_id[b], 1.0, rel_tol=1e-9)


def test_dram_only_samples_yield_rapl_dram_source(tmp_path):
    run_id = new_ulid()
    (tmp_path / "capsule.yaml").write_text(f"run_id: {run_id}\n")
    mc = new_ulid()
    _write_jsonl(tmp_path / "model-calls.jsonl", [{"model_call_id": mc}])
    _write_jsonl(
        tmp_path / "energy-samples.jsonl",
        [
            {"ts": 1.0, "domains_uj": {"intel-rapl:0:0-dram": 0}, "gap": False},
            {"ts": 2.0, "domains_uj": {"intel-rapl:0:0-dram": 1_000_000}, "gap": False},
        ],
    )
    receipts = attest_capsule_with_samples(tmp_path, node_id="n1")
    assert len(receipts) == 1
    assert receipts[0].measurement_source is MeasurementSource.RAPL_DRAM
    assert receipts[0].consistency_errors() == []


def test_malformed_iso_timestamps_fall_back_to_even_split(tmp_path):
    run_id = new_ulid()
    (tmp_path / "capsule.yaml").write_text(f"run_id: {run_id}\n")
    a, b = new_ulid(), new_ulid()
    # unparseable started/finished → wall-clock None for both → even split
    _write_jsonl(
        tmp_path / "tool-calls.jsonl",
        [
            {"tool_call_id": a, "started": "not-a-time", "finished": "also-bad"},
            {"tool_call_id": b, "started": "nope", "finished": "nope"},
        ],
    )
    _write_jsonl(
        tmp_path / "energy-samples.jsonl",
        [
            {"ts": 1.0, "domains_uj": {"intel-rapl:0": 0}, "gap": False},
            {"ts": 2.0, "domains_uj": {"intel-rapl:0": 2_000_000}, "gap": False},
        ],
    )
    receipts = attest_capsule_with_samples(tmp_path, node_id="n1")
    for r in receipts:
        assert r.measured_joules is not None
        assert math.isclose(r.measured_joules, 1.0, rel_tol=1e-9)  # 2.0 J / 2


def test_samples_present_but_no_actions_returns_empty(tmp_path):
    run_id = new_ulid()
    (tmp_path / "capsule.yaml").write_text(f"run_id: {run_id}\n")
    # samples but no action streams at all
    _write_jsonl(
        tmp_path / "energy-samples.jsonl",
        [
            {"ts": 1.0, "domains_uj": {"intel-rapl:0": 0}, "gap": False},
            {"ts": 2.0, "domains_uj": {"intel-rapl:0": 1_000}, "gap": False},
        ],
    )
    assert attest_capsule_with_samples(tmp_path, node_id="n1") == []


def test_attest_capsule_dispatches_to_samples_when_present(tmp_path):
    # the public attest_capsule should auto-detect samples and apportion
    capsule, _, ids, node_joules = _make_capsule_with_samples(
        tmp_path, start_uj=0, end_uj=2_000_000, durations_ms=(100.0, 100.0)
    )
    receipts = attest_capsule(capsule, node_id="n1")
    assert all(r.confidence is Confidence.APPORTIONED for r in receipts)
    total = sum(r.measured_joules for r in receipts if r.measured_joules is not None)
    assert math.isclose(total, node_joules, rel_tol=1e-9)
