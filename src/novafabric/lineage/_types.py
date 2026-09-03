from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from novafabric.capture._ulid import new_ulid


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def node_id_for(kind: str, ref: str) -> str:
    return hashlib.sha256(f"{kind}:{ref}".encode()).hexdigest()[:26]


def node_ref_for_run(run_id: str) -> str:
    return run_id


def node_ref_for_asset(registry: str, asset_ref: str) -> str:
    return f"{registry}:{asset_ref}"


def node_ref_for_artifact(capsule_run_id: str, path: str) -> str:
    return f"artifact:{capsule_run_id}:{path}"


@dataclass
class LineageNode:
    node_id: str
    kind: str
    ref: str
    first_seen_capsule_run_id: str | None
    payload: dict[str, Any]


def node_ref_from_edge_dict(node_dict: dict[str, Any]) -> str:
    """Return the canonical ``ref`` for a raw edge endpoint dict.

    Every backend keys its nodes on ``(kind, ref)``, so this mapping *is* node
    identity. It lived as four separate copies — SQLite, Postgres, AGE and the
    Kuzu backend each had their own — which is exactly how the Kuzu backend came
    to store an asset as a run with an empty ref (see ADR 0266).
    """
    kind = node_dict.get("kind", "")
    if kind == "run":
        return str(node_dict.get("run_id", ""))
    if kind == "asset":
        return node_ref_for_asset(
            str(node_dict.get("registry", "local")),
            str(node_dict.get("asset_ref", "")),
        )
    if kind == "artifact":
        artifact_ref = node_dict.get("artifact_ref", {}) or {}
        return node_ref_for_artifact(
            str(artifact_ref.get("capsule_run_id", "")),
            str(artifact_ref.get("path", "")),
        )
    return str(node_dict.get("ref", str(node_dict)))


def node_from_edge_dict(node_dict: dict[str, Any]) -> LineageNode:
    """Resolve a raw edge endpoint dict into a :class:`LineageNode`.

    The single definition of node identity shared by every backend.
    """
    kind = str(node_dict.get("kind", ""))
    ref = node_ref_from_edge_dict(node_dict)
    return LineageNode(
        node_id=node_id_for(kind, ref),
        kind=kind,
        ref=ref,
        first_seen_capsule_run_id=node_dict.get("capsule_run_id"),
        payload=node_dict,
    )


@dataclass
class LineageEdge:
    edge_type: str
    source: dict[str, Any]
    target: dict[str, Any]
    confidence: str
    capsule_run_id: str
    schema_version: str = "0.1.0"
    edge_id: str = field(default_factory=new_ulid)
    direction: str = "source_to_target"
    created_at: str = field(default_factory=_now)
    emitter: dict[str, Any] = field(
        default_factory=lambda: {"name": "novafabric", "version": "0.4.0"}
    )
    facets: dict[str, Any] | None = None
    evidence_refs: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "edge_id": self.edge_id,
            "edge_type": self.edge_type,
            "source": self.source,
            "target": self.target,
            "direction": self.direction,
            "created_at": self.created_at,
            "emitter": self.emitter,
            "confidence": self.confidence,
            "capsule_run_id": self.capsule_run_id,
        }
        if self.facets is not None:
            d["facets"] = self.facets
        if self.evidence_refs is not None:
            d["evidence_refs"] = self.evidence_refs
        return d
