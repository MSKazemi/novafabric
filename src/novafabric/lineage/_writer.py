# src/novafabric/lineage/_writer.py
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from novafabric._capsule_fs import read_capsule_member_or_none
from novafabric.lineage._facets import attach_column_facets, facets_from_tool_calls
from novafabric.lineage._types import LineageEdge, default_emitter

#: Optional Artifact fields carried into ``artifact_ref`` (identity stays ``path``).
_ARTIFACT_HINTS = ("name", "content_hash", "media_type")


def _output_artifact_ref(entry: object, run_id: str) -> dict[str, Any] | None:
    """``artifact_ref`` for one manifest ``outputs[]`` entry, or ``None`` to skip it.

    The run-capsule schema makes each entry an Artifact object (``name``, ``path``,
    ``content_hash``, …); older capsules list bare path strings. An inline
    Artifact has no meaningful ``path`` (the schema says it is ignored), so it is
    keyed ``inline:<name>``. Anything else carries no usable path and is skipped
    rather than stringified into a node ref (L14).
    """
    if isinstance(entry, str):
        return {"capsule_run_id": run_id, "path": entry} if entry else None
    if not isinstance(entry, dict):
        return None
    name = entry.get("name")
    path = entry.get("path")
    if entry.get("inline") is True:
        path = f"inline:{name}" if isinstance(name, str) and name else None
    if not isinstance(path, str) or not path:
        return None
    ref: dict[str, Any] = {"capsule_run_id": run_id, "path": path}
    for key in _ARTIFACT_HINTS:
        value = entry.get(key)
        if isinstance(value, str) and value:
            ref[key] = value
    return ref


class LineageWriter:
    def __init__(self, capsule_dir: Path, run_id: str, version: str | None = None) -> None:
        self._capsule_dir = capsule_dir
        self._run_id = run_id
        self._version = version if version is not None else default_emitter()["version"]

    def infer(self) -> list[LineageEdge]:
        edges: list[LineageEdge] = []
        edges.extend(self._consumed_edges())
        edges.extend(self._produced_by_edges())
        replay_edge = self._replayed_from_edge()
        if replay_edge is not None:
            edges.append(replay_edge)
        # ADR-0090 Half 1 — column facets from captured SQL (fail-open).
        attach_column_facets(edges, facets_from_tool_calls(self._capsule_dir))
        return edges

    def write(self, edges: list[LineageEdge]) -> Path:
        path = self._capsule_dir / "lineage.jsonl"
        lines = [json.dumps(e.as_dict(), ensure_ascii=False) for e in edges]
        path.write_text("\n".join(lines) + ("\n" if lines else ""))
        return path

    def _consumed_edges(self) -> list[LineageEdge]:
        assets_path = self._capsule_dir / "assets.jsonl"
        if not assets_path.exists():
            return []
        edges: list[LineageEdge] = []
        capsule_text = (
            read_capsule_member_or_none(assets_path, operation="lineage") or ""
        )
        for line in capsule_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            asset_ref = record.get("asset_ref", "")
            registry = record.get("registry", "local")
            if not asset_ref:
                continue
            # C-1.1 — propagate status_at_consumption into edge facets when
            # the consumption record was written with that field present.
            status_at_consumption = record.get("status_at_consumption")
            facets: dict[str, Any] | None = None
            if status_at_consumption is not None:
                facets = {"status_at_consumption": status_at_consumption}
            edges.append(
                LineageEdge(
                    edge_type="consumed",
                    source={"kind": "run", "run_id": self._run_id},
                    target={"kind": "asset", "asset_ref": asset_ref, "registry": registry},
                    confidence="observed",
                    capsule_run_id=self._run_id,
                    emitter={"name": "novafabric", "version": self._version},
                    evidence_refs=["assets.jsonl"],
                    facets=facets,
                )
            )
        return edges

    def _produced_by_edges(self) -> list[LineageEdge]:
        edges: list[LineageEdge] = []
        edges.extend(self._produced_by_edges_from_manifest())
        edges.extend(self._produced_by_edges_from_file())
        return edges

    def _produced_by_edges_from_manifest(self) -> list[LineageEdge]:
        manifest_path = self._capsule_dir / "capsule.yaml"
        if not manifest_path.exists():
            return []
        try:
            capsule_text = (
                read_capsule_member_or_none(manifest_path, operation="lineage") or ""
            )
            manifest: Any = yaml.safe_load(capsule_text) or {}
        except Exception:
            return []
        outputs = manifest.get("outputs") if isinstance(manifest, dict) else None
        if not isinstance(outputs, list):
            return []
        edges: list[LineageEdge] = []
        for entry in outputs:
            artifact_ref = _output_artifact_ref(entry, self._run_id)
            if artifact_ref is None:
                continue
            edges.append(
                LineageEdge(
                    edge_type="produced_by",
                    source={"kind": "artifact", "artifact_ref": artifact_ref},
                    target={"kind": "run", "run_id": self._run_id},
                    confidence="inferred",
                    capsule_run_id=self._run_id,
                    emitter={"name": "novafabric", "version": self._version},
                    evidence_refs=["capsule.yaml"],
                )
            )
        return edges

    def _produced_by_edges_from_file(self) -> list[LineageEdge]:
        """Read produced.jsonl written by record_produced() and emit produced_by edges."""
        produced_path = self._capsule_dir / "produced.jsonl"
        if not produced_path.exists():
            return []
        edges: list[LineageEdge] = []
        capsule_text = (
            read_capsule_member_or_none(produced_path, operation="lineage") or ""
        )
        for line in capsule_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = record.get("path", "")
            if not path:
                continue
            edges.append(
                LineageEdge(
                    edge_type="produced_by",
                    source={"kind": "artifact",
                            "artifact_ref": {
                                "capsule_run_id": self._run_id,
                                "path": path,
                            }},
                    target={"kind": "run", "run_id": self._run_id},
                    confidence="declared",
                    capsule_run_id=self._run_id,
                    emitter={"name": "novafabric", "version": self._version},
                    evidence_refs=["produced.jsonl"],
                )
            )
        return edges

    def _replayed_from_edge(self) -> LineageEdge | None:
        manifest_path = self._capsule_dir / "capsule.yaml"
        if not manifest_path.exists():
            return None
        try:
            capsule_text = (
                read_capsule_member_or_none(manifest_path, operation="lineage") or ""
            )
            manifest: dict[str, Any] = yaml.safe_load(capsule_text) or {}
        except Exception:
            return None
        original_run_id: str = manifest.get("replay_of_run_id", "") or ""
        if not original_run_id:
            return None
        return LineageEdge(
            edge_type="replayed_from",
            source={"kind": "run", "run_id": self._run_id},
            target={"kind": "run", "run_id": original_run_id},
            confidence="declared",
            capsule_run_id=self._run_id,
            emitter={"name": "novafabric", "version": self._version},
            evidence_refs=["capsule.yaml"],
        )
