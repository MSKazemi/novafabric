"""Agent execution graph — within-run DAG projection over one capsule (ADR-0124).

A deterministic, read-only, content-addressable reconstruction of a single
agentic run's control flow from records the capsule already holds
(``model-calls.jsonl`` + ``tool-calls.jsonl`` + the OTel span tree in
``trace.jsonl``). Projection, not capture: no capsule field is added, no record
is written at run time, and nothing is inferred beyond what the spans encode —
gaps attach to a synthetic root with explicit ``reconstruction_notes``.

Distinct from cross-run lineage (ADR-0090) and the fleet SPKG (ADR-0111).
Spec: ``design/spec/agent-execution-graph-v0.md``. Status: **experimental**.
"""

from novafabric.agent_graph.builder import build_agent_graph
from novafabric.agent_graph.errors import AgentGraphError, CapsuleNotFoundError
from novafabric.agent_graph.export import to_dot, to_mermaid
from novafabric.agent_graph.model import (
    GRAPH_SCHEMA_VERSION,
    ROOT_NODE_ID,
    AgentExecutionGraph,
    GraphEdge,
    GraphNode,
    GraphStats,
    ReconstructionNote,
    canonical_payload,
    compute_graph_digest,
)

__all__ = [
    "GRAPH_SCHEMA_VERSION",
    "ROOT_NODE_ID",
    "AgentExecutionGraph",
    "AgentGraphError",
    "CapsuleNotFoundError",
    "GraphEdge",
    "GraphNode",
    "GraphStats",
    "ReconstructionNote",
    "build_agent_graph",
    "canonical_payload",
    "compute_graph_digest",
    "to_dot",
    "to_mermaid",
]
