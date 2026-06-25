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
