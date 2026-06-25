# src/novafabric/lineage/_openlineage.py
from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

_log = logging.getLogger(__name__)

_NOVAFABRIC_NS = uuid.UUID("7b3f4e2a-1c0d-5b8e-9f6a-4d2e1a0c3b7f")
_PRODUCER = "https://github.com/novafabric/novafabric"
_FACET_PRODUCER = "https://novafabric.io"
_SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
_NOMINAL_TIME_SCHEMA = "https://openlineage.io/spec/facets/1-0-0/NominalTimeRunFacet.json"
_JOB_TYPE_SCHEMA = "https://openlineage.io/spec/facets/2-0-2/JobTypeJobFacet.json"
_PARENT_FACET_SCHEMA = "https://openlineage.io/spec/facets/1-0-0/ParentRunFacet.json"

# DO NOT MUTATE — shared by reference in all emitted events
_JOB_TYPE_FACET: dict[str, Any] = {
    "_producer": _FACET_PRODUCER,
    "_schemaURL": _JOB_TYPE_SCHEMA,
    "processingType": "BATCH",
    "integration": "NOVAFABRIC",
    "jobType": "CAPTURE",
}


def _stable_job_name(command: list[str]) -> str:
    slug0 = re.sub(r"[^a-z0-9]+", ".", command[0].lower()).strip(".")
    if len(command) > 1 and not command[1].startswith("-"):
        slug1 = re.sub(r"[^a-z0-9]+", ".", command[1].lower()).strip(".")
        return f"{slug0}.{slug1}"
    return slug0


def _run_uuid(run_id: str) -> str:
    return str(uuid.uuid5(_NOVAFABRIC_NS, run_id))


def build_start_event(run_id: str, command: list[str], started_at: str) -> dict[str, Any]:
    return {
        "eventType": "START",
        "eventTime": started_at,
        "producer": _PRODUCER,
        "schemaURL": _SCHEMA_URL,
        "run": {
            "runId": _run_uuid(run_id),
            "facets": {
                "nominalTime": {
                    "_producer": _FACET_PRODUCER,
                    "_schemaURL": _NOMINAL_TIME_SCHEMA,
                    "nominalStartTime": started_at,
                },
            },
        },
        "job": {
            "namespace": "novafabric",
            "name": _stable_job_name(command),
            "facets": {"jobType": _JOB_TYPE_FACET},
        },
        "inputs": [],
        "outputs": [],
    }


def build_complete_event(
    run_id: str,
    command: list[str],
    capsule_dir: Path,
    exit_code: int,
    finished_at: str,
    started_at: str,
) -> dict[str, Any]:
    event_type = "COMPLETE" if exit_code == 0 else "FAIL"
    job_name = _stable_job_name(command)
    run_facets: dict[str, Any] = {
        "nominalTime": {
            "_producer": _FACET_PRODUCER,
            "_schemaURL": _NOMINAL_TIME_SCHEMA,
            "nominalStartTime": started_at,
            "nominalEndTime": finished_at,
        },
    }
    inputs: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []

    lineage_path = capsule_dir / "lineage.jsonl"
    if lineage_path.exists():
        for line in lineage_path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                edge: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            edge_type = edge.get("edge_type", "")
            if edge_type == "replayed_from":
                tgt = edge.get("target", {})
                parent_run_id = tgt.get("run_id", "")
                if parent_run_id:
                    run_facets["parent"] = {
                        "_producer": _FACET_PRODUCER,
                        "_schemaURL": _PARENT_FACET_SCHEMA,
                        "run": {"runId": _run_uuid(parent_run_id)},
                        "job": {"namespace": "novafabric", "name": job_name},
                    }
            elif edge_type == "consumed":
                tgt = edge.get("target", {})
                asset_ref = tgt.get("asset_ref", "")
                registry = tgt.get("registry", "local")
                if asset_ref:
                    inputs.append({"namespace": registry, "name": asset_ref, "facets": {}})
            elif edge_type == "produced_by":
                src = edge.get("source", {})
                artifact_ref = src.get("artifact_ref", {})
                if artifact_ref:
                    path = artifact_ref.get("path", "")
                    crun_id = artifact_ref.get("capsule_run_id", "")
                    name = f"artifact:{crun_id}:{path}" if path else ""
                    if name:
                        outputs.append({"namespace": "novafabric", "name": name, "facets": {}})

    return {
        "eventType": event_type,
        "eventTime": finished_at,
        "producer": _PRODUCER,
        "schemaURL": _SCHEMA_URL,
        "run": {
            "runId": _run_uuid(run_id),
            "facets": run_facets,
        },
        "job": {
            "namespace": "novafabric",
            "name": job_name,
            "facets": {"jobType": _JOB_TYPE_FACET},
        },
        "inputs": inputs,
        "outputs": outputs,
    }


def build_events_from_capsule(capsule_dir: Path) -> list[dict[str, Any]]:
    capsule_yaml = capsule_dir / "capsule.yaml"
    if not capsule_yaml.exists():
        return []
    try:
        manifest: dict[str, Any] = yaml.safe_load(capsule_yaml.read_text()) or {}
    except Exception as exc:
        _log.warning("Failed to parse capsule.yaml in %s: %s", capsule_dir, exc)
        return []
    run_id: str = manifest.get("run_id", "")
    command: list[str] = manifest.get("command", []) or []
    started_at: str = manifest.get("created_at", "")
    finished_at: str = manifest.get("finished_at", "")
    try:
        exit_code: int = int(manifest.get("exit_code", 0))
    except (TypeError, ValueError):
        exit_code = 0
    if not run_id or not command:
        return []
    return [
        build_start_event(run_id=run_id, command=command, started_at=started_at),
        build_complete_event(
            run_id=run_id,
            command=command,
            capsule_dir=capsule_dir,
            exit_code=exit_code,
            finished_at=finished_at,
            started_at=started_at,
        ),
    ]
