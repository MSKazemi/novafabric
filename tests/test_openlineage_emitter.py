# tests/test_openlineage_emitter.py
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import yaml


def test_stable_job_name_excludes_flags() -> None:
    from novafabric.lineage._openlineage import _stable_job_name
    assert _stable_job_name(["python", "train.py", "--lr", "0.01"]) == "python.train.py"


def test_stable_job_name_same_for_different_args() -> None:
    from novafabric.lineage._openlineage import _stable_job_name
    assert _stable_job_name(["python", "train.py", "--lr", "0.01"]) == \
           _stable_job_name(["python", "train.py", "--lr", "0.001"])


def test_stable_job_name_flag_second_arg() -> None:
    from novafabric.lineage._openlineage import _stable_job_name
    assert _stable_job_name(["python", "--version"]) == "python"


def test_stable_job_name_single_command() -> None:
    from novafabric.lineage._openlineage import _stable_job_name
    assert _stable_job_name(["myapp"]) == "myapp"


def test_run_uuid_is_deterministic() -> None:
    from novafabric.lineage._openlineage import _run_uuid
    assert _run_uuid("01FAKEULIDAAA001") == _run_uuid("01FAKEULIDAAA001")


def test_run_uuid_is_valid_uuid() -> None:
    from novafabric.lineage._openlineage import _run_uuid
    parsed = uuid.UUID(_run_uuid("01FAKEULIDAAA001"))
    assert parsed.version == 5


def test_run_uuid_different_ids_differ() -> None:
    from novafabric.lineage._openlineage import _run_uuid
    assert _run_uuid("AAAA") != _run_uuid("BBBB")


def test_build_start_event_structure() -> None:
    from novafabric.lineage._openlineage import _run_uuid, build_start_event
    ev = build_start_event("RUN1", ["python", "train.py"], "2026-05-08T12:00:00.000000Z")
    assert ev["eventType"] == "START"
    assert ev["run"]["runId"] == _run_uuid("RUN1")
    assert ev["job"]["namespace"] == "novafabric"
    assert ev["job"]["name"] == "python.train.py"
    assert ev["inputs"] == []
    assert ev["outputs"] == []
    assert "nominalTime" in ev["run"]["facets"]
    assert ev["run"]["facets"]["nominalTime"]["nominalStartTime"] == "2026-05-08T12:00:00.000000Z"
    assert "jobType" in ev["job"]["facets"]


def test_build_complete_event_with_consumed_edge(tmp_path: Path) -> None:
    from novafabric.lineage._openlineage import build_complete_event
    edge: dict[str, Any] = {
        "edge_type": "consumed",
        "source": {"kind": "run", "run_id": "RUN1"},
        "target": {"kind": "asset", "asset_ref": "train.parquet", "registry": "s3"},
    }
    (tmp_path / "lineage.jsonl").write_text(json.dumps(edge) + "\n")
    ev = build_complete_event("RUN1", ["python", "train.py"], tmp_path, 0,
                              "2026-05-08T12:01:00.000000Z", "2026-05-08T12:00:00.000000Z")
    assert ev["eventType"] == "COMPLETE"
    assert len(ev["inputs"]) == 1
    assert ev["inputs"][0]["namespace"] == "s3"
    assert ev["inputs"][0]["name"] == "train.parquet"


def test_build_complete_event_with_produced_by_edge(tmp_path: Path) -> None:
    from novafabric.lineage._openlineage import build_complete_event
    edge: dict[str, Any] = {
        "edge_type": "produced_by",
        "source": {
            "kind": "artifact",
            "artifact_ref": {"capsule_run_id": "RUN1", "path": "out/result.json"},
        },
        "target": {"kind": "run", "run_id": "RUN1"},
    }
    (tmp_path / "lineage.jsonl").write_text(json.dumps(edge) + "\n")
    ev = build_complete_event("RUN1", ["python", "train.py"], tmp_path, 0,
                              "2026-05-08T12:01:00.000000Z", "2026-05-08T12:00:00.000000Z")
    assert len(ev["outputs"]) == 1
    assert ev["outputs"][0]["name"] == "artifact:RUN1:out/result.json"


def test_build_complete_event_with_replayed_from_edge(tmp_path: Path) -> None:
    from novafabric.lineage._openlineage import _run_uuid, build_complete_event
    edge: dict[str, Any] = {
        "edge_type": "replayed_from",
        "source": {"kind": "run", "run_id": "RUN1"},
        "target": {"kind": "run", "run_id": "RUN0"},
    }
    (tmp_path / "lineage.jsonl").write_text(json.dumps(edge) + "\n")
    ev = build_complete_event("RUN1", ["python"], tmp_path, 0,
                              "2026-05-08T12:01:00.000000Z", "2026-05-08T12:00:00.000000Z")
    assert "parent" in ev["run"]["facets"]
    assert ev["run"]["facets"]["parent"]["run"]["runId"] == _run_uuid("RUN0")


def test_build_complete_event_fail_on_nonzero_exit(tmp_path: Path) -> None:
    from novafabric.lineage._openlineage import build_complete_event
    (tmp_path / "lineage.jsonl").write_text("")
    ev = build_complete_event("RUN1", ["python"], tmp_path, 1,
                              "2026-05-08T12:01:00.000000Z", "2026-05-08T12:00:00.000000Z")
    assert ev["eventType"] == "FAIL"


def test_build_complete_event_nominal_time_facet(tmp_path: Path) -> None:
    from novafabric.lineage._openlineage import build_complete_event
    (tmp_path / "lineage.jsonl").write_text("")
    ev = build_complete_event("RUN1", ["python"], tmp_path, 0,
                              "2026-05-08T12:01:00.000000Z", "2026-05-08T12:00:00.000000Z")
    nt = ev["run"]["facets"]["nominalTime"]
    assert nt["nominalStartTime"] == "2026-05-08T12:00:00.000000Z"
    assert nt["nominalEndTime"] == "2026-05-08T12:01:00.000000Z"


def _write_capsule(path: Path, run_id: str = "RUN1", exit_code: int = 0) -> None:
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "command": ["python", "train.py"],
        "created_at": "2026-05-08T12:00:00.000000Z",
        "finished_at": "2026-05-08T12:01:00.000000Z",
        "exit_code": exit_code,
        "status": "success" if exit_code == 0 else "failure",
    }
    (path / "capsule.yaml").write_text(yaml.dump(manifest))
    (path / "lineage.jsonl").write_text("")


def test_build_events_from_capsule_returns_start_and_complete(tmp_path: Path) -> None:
    from novafabric.lineage._openlineage import build_events_from_capsule
    _write_capsule(tmp_path)
    events = build_events_from_capsule(tmp_path)
    assert len(events) == 2
    assert events[0]["eventType"] == "START"
    assert events[1]["eventType"] == "COMPLETE"


def test_build_events_from_capsule_returns_fail_for_failed_capsule(tmp_path: Path) -> None:
    from novafabric.lineage._openlineage import build_events_from_capsule
    _write_capsule(tmp_path, exit_code=1)
    events = build_events_from_capsule(tmp_path)
    assert events[1]["eventType"] == "FAIL"


def test_build_events_from_capsule_no_lineage_jsonl(tmp_path: Path) -> None:
    from novafabric.lineage._openlineage import build_events_from_capsule
    _write_capsule(tmp_path)
    (tmp_path / "lineage.jsonl").unlink()
    events = build_events_from_capsule(tmp_path)
    assert len(events) == 2
    assert events[1]["inputs"] == []
    assert events[1]["outputs"] == []


def test_build_events_from_capsule_no_capsule_yaml(tmp_path: Path) -> None:
    from novafabric.lineage._openlineage import build_events_from_capsule
    events = build_events_from_capsule(tmp_path)
    assert events == []
