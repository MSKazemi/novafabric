"""Agent execution graph — typed model + canonicalisation + digest (ADR-0124).

The graph is a deterministic, read-only, content-addressable projection of one
Run Capsule's already-captured records (``model-calls.jsonl`` +
``tool-calls.jsonl`` + the OTel span tree in ``trace.jsonl``). Nothing here
touches the capture path; the model mirrors
``schemas/agent-execution-graph.schema.json`` and the canonicalisation rules in
``design/spec/agent-execution-graph-v0.md`` §Canonicalisation.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

GRAPH_SCHEMA_VERSION = "0.1.0"

NodeKind = Literal["model_call", "tool_call", "span", "root"]
NodeStatus = Literal["success", "error", "timeout", "denied", "partial"]
EdgeType = Literal["span_parent", "agent_invokes_tool", "follows"]
NoteKind = Literal["missing_parent", "orphan_tool_call", "unlinked_span"]

#: Node ``status`` values a source record may contribute (spec: the node status
#: *mirrors* the source record's status where present — never remapped).
NODE_STATUS_VALUES: frozenset[str] = frozenset(
    ("success", "error", "timeout", "denied", "partial")
)

#: Reserved id of the synthetic root node.
ROOT_NODE_ID = "root"


class GraphNode(BaseModel):
    """One node of the execution DAG (a model call, tool call, span, or root)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    kind: NodeKind
    label: str
    started_at: str | None
    duration_ms: int | None = Field(ge=0)
    status: NodeStatus | None = None
    mutation_class: str | None = None
    source_ref: str | None = None

    def to_document(self) -> dict[str, Any]:
        """JSON document form: nullable required fields kept, absent optionals dropped."""
        doc: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
        }
        if self.status is not None:
            doc["status"] = self.status
        if self.mutation_class is not None:
            doc["mutation_class"] = self.mutation_class
        if self.source_ref is not None:
            doc["source_ref"] = self.source_ref
        return doc


class GraphEdge(BaseModel):
    """One directed edge; exactly the three deterministic ADR-0124 edge types."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    type: EdgeType
    from_: str = Field(alias="from", min_length=1)
    to: str = Field(min_length=1)

    def to_document(self) -> dict[str, Any]:
        return {"type": self.type, "from": self.from_, "to": self.to}


def make_edge(edge_type: EdgeType, source: str, target: str) -> GraphEdge:
    """Typed constructor (the ``from`` alias is not a valid Python keyword)."""
    return GraphEdge.model_validate({"type": edge_type, "from": source, "to": target})


class ReconstructionNote(BaseModel):
    """An honestly recorded gap — never a silent heuristic repair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: NoteKind
    node_id: str
    detail: str

    def to_document(self) -> dict[str, Any]:
        return {"kind": self.kind, "node_id": self.node_id, "detail": self.detail}


class GraphStats(BaseModel):
    """Advisory shape summary; excluded from ``graph_digest``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_count: int = Field(ge=0)
    edge_count: int = Field(ge=0)
    max_depth: int = Field(ge=0)
    max_fan_out: int = Field(ge=0)

    def to_document(self) -> dict[str, Any]:
        return {
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "max_depth": self.max_depth,
            "max_fan_out": self.max_fan_out,
        }


def _node_sort_key(node: GraphNode) -> bytes:
    return node.id.encode("utf-8")


def _edge_sort_key(edge: GraphEdge) -> tuple[bytes, bytes, bytes]:
    return (
        edge.type.encode("utf-8"),
        edge.from_.encode("utf-8"),
        edge.to.encode("utf-8"),
    )


def canonical_payload(
    capsule_id: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    schema_version: str = GRAPH_SCHEMA_VERSION,
) -> dict[str, Any]:
    """The digest input: ``{schema_version, capsule_id, nodes, edges}``, canonically sorted.

    ``stats`` and ``reconstruction_notes`` are advisory/derived and MUST be
    excluded (spec §Canonicalisation).
    """
    return {
        "schema_version": schema_version,
        "capsule_id": capsule_id,
        "nodes": [n.to_document() for n in sorted(nodes, key=_node_sort_key)],
        "edges": [e.to_document() for e in sorted(edges, key=_edge_sort_key)],
    }


def compute_graph_digest(
    capsule_id: str,
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    *,
    schema_version: str = GRAPH_SCHEMA_VERSION,
) -> str:
    """``"sha256:" + hex(SHA-256(canonical bytes))`` per the spec's canonicalisation."""
    payload = canonical_payload(
        capsule_id, nodes, edges, schema_version=schema_version
    )
    blob = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


class AgentExecutionGraph(BaseModel):
    """The content-addressed execution DAG of one captured run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = GRAPH_SCHEMA_VERSION
    capsule_id: str = Field(min_length=1)
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    graph_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    reconstruction_notes: list[ReconstructionNote] | None = None
    stats: GraphStats | None = None

    @classmethod
    def assemble(
        cls,
        capsule_id: str,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        notes: list[ReconstructionNote] | None = None,
        stats: GraphStats | None = None,
    ) -> AgentExecutionGraph:
        """Canonically sort, digest, and freeze a reconstructed graph."""
        sorted_nodes = sorted(nodes, key=_node_sort_key)
        sorted_edges = sorted(edges, key=_edge_sort_key)
        sorted_notes = (
            sorted(notes, key=lambda n: (n.node_id, n.kind, n.detail))
            if notes
            else None
        )
        return cls(
            capsule_id=capsule_id,
            nodes=sorted_nodes,
            edges=sorted_edges,
            graph_digest=compute_graph_digest(capsule_id, sorted_nodes, sorted_edges),
            reconstruction_notes=sorted_notes,
            stats=stats,
        )

    def to_document(self) -> dict[str, Any]:
        """The full canonical JSON document (schema-valid; notes/stats when present)."""
        doc = canonical_payload(
            self.capsule_id,
            self.nodes,
            self.edges,
            schema_version=self.schema_version,
        )
        doc["graph_digest"] = self.graph_digest
        if self.reconstruction_notes:
            doc["reconstruction_notes"] = [
                n.to_document() for n in self.reconstruction_notes
            ]
        if self.stats is not None:
            doc["stats"] = self.stats.to_document()
        return doc
