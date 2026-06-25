# src/novafabric/lineage/_importer.py
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novafabric.lineage._store import LineageStore
from novafabric.lineage._types import (
    LineageEdge,
    LineageNode,
    node_id_for,
    node_ref_for_artifact,
    node_ref_for_asset,
    node_ref_for_run,
)

# Load schema once at module level (cheap — small JSON file).
_SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "lineage-edge.schema.json"
_SCHEMA = json.loads(_SCHEMA_PATH.read_text())


def _validate_edge_record(record: dict[str, Any]) -> None:
    """Validate an edge record against the lineage-edge schema.

    ``jsonschema`` is imported here (not at module top) so that callers
    that import :mod:`._importer` but never call this function — e.g.
    capsules with no lineage edges — pay zero ``jsonschema`` import cost.
    Hot-path optimization (v0.6.8 benchmark finding): jsonschema's
    top-level import accounted for ~57 ms per orchestrator invocation.
    """
    import jsonschema  # type: ignore[import-untyped]
    jsonschema.validate(record, _SCHEMA)


def _edge_from_dict(record: dict[str, Any]) -> LineageEdge:
    return LineageEdge(
        schema_version=record.get("schema_version", "0.1.0"),
        edge_id=record["edge_id"],
        edge_type=record["edge_type"],
        source=record["source"],
        target=record["target"],
        direction=record.get("direction", "source_to_target"),
        created_at=record["created_at"],
        emitter=record.get("emitter", {"name": "novafabric", "version": "unknown"}),
        confidence=record.get("confidence", "observed"),
        capsule_run_id=record.get("capsule_run_id", ""),
        facets=record.get("facets"),
        evidence_refs=record.get("evidence_refs"),
    )


def _node_from_node_dict(
    node_dict: dict[str, Any], capsule_run_id: str
) -> LineageNode:
    kind = node_dict.get("kind", "")
    if kind == "run":
        ref = node_ref_for_run(node_dict.get("run_id", ""))
    elif kind == "asset":
        ref = node_ref_for_asset(
            node_dict.get("registry", "local"),
            node_dict.get("asset_ref", ""),
        )
    elif kind == "artifact":
        artifact_ref = node_dict.get("artifact_ref", {})
        ref = node_ref_for_artifact(
            artifact_ref.get("capsule_run_id", capsule_run_id),
            artifact_ref.get("path", ""),
        )
    else:
        ref = str(node_dict)
    return LineageNode(
        node_id=node_id_for(kind, ref),
        kind=kind,
        ref=ref,
        first_seen_capsule_run_id=capsule_run_id,
        payload=node_dict,
    )


@dataclass
class ImportResult:
    capsule_run_id: str
    edges_indexed: int
    nodes_indexed: int
    skipped: bool
    warning: str | None = None


def index_capsule_lineage(
    capsule_dir: Path,
    db_path: Path | None = None,
) -> ImportResult:
    lineage_path = capsule_dir / "lineage.jsonl"
    capsule_run_id = capsule_dir.name

    if not lineage_path.exists():
        return ImportResult(
            capsule_run_id=capsule_run_id,
            edges_indexed=0,
            nodes_indexed=0,
            skipped=True,
        )

    try:
        edges: list[LineageEdge] = []
        nodes_by_id: dict[str, LineageNode] = {}

        for line in lineage_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            record: dict[str, Any] = json.loads(line)
            _validate_edge_record(record)
            edge = _edge_from_dict(record)
            run_id_for_node = record.get("capsule_run_id", capsule_dir.name)
            edges.append(edge)
            for node_dict in (record["source"], record["target"]):
                node = _node_from_node_dict(node_dict, run_id_for_node)
                nodes_by_id[node.node_id] = node

    except (  # noqa: E501
        json.JSONDecodeError,
        # jsonschema.ValidationError is caught here too — its base class
        # is ValueError so the existing handler covers it without
        # forcing the jsonschema import at module load time.
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        return ImportResult(
            capsule_run_id=capsule_run_id,
            edges_indexed=0,
            nodes_indexed=0,
            skipped=True,
            warning=str(exc),
        )

    # Always insert the run node from capsule.yaml so it is queryable even when
    # there are no edges (e.g. a run with no inputs/outputs/assets).
    run_node = LineageNode(
        node_id=node_id_for("run", node_ref_for_run(capsule_run_id)),
        kind="run",
        ref=node_ref_for_run(capsule_run_id),
        first_seen_capsule_run_id=capsule_run_id,
        payload={"kind": "run", "run_id": capsule_run_id},
    )
    nodes_by_id.setdefault(run_node.node_id, run_node)

    if not edges:
        nodes = list(nodes_by_id.values())
        store = LineageStore(db_path=db_path)
        store.replace_capsule_lineage(
            nodes=nodes, edges=[], capsule_run_id=capsule_run_id
        )
        return ImportResult(
            capsule_run_id=capsule_run_id,
            edges_indexed=0,
            nodes_indexed=len(nodes),
            skipped=False,
        )

    nodes = list(nodes_by_id.values())
    store = LineageStore(db_path=db_path)
    store.replace_capsule_lineage(
        nodes=nodes, edges=edges, capsule_run_id=capsule_run_id
    )

    return ImportResult(
        capsule_run_id=capsule_run_id,
        edges_indexed=len(edges),
        nodes_indexed=len(nodes),
        skipped=False,
    )


def import_capsule_dir(
    runs_dir: Path,
    db_path: Path | None = None,
) -> list[ImportResult]:
    results: list[ImportResult] = []
    for entry in sorted(runs_dir.iterdir()):
        if entry.is_dir() and (entry / "capsule.yaml").exists():
            results.append(index_capsule_lineage(entry, db_path=db_path))
    return results
