"""Model + canonicalisation tests: digest determinism against the golden fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from novafabric.agent_graph import (
    AgentExecutionGraph,
    GraphEdge,
    GraphNode,
    GraphStats,
    ReconstructionNote,
    canonical_payload,
    compute_graph_digest,
)

FIXTURES = (
    Path(__file__).resolve().parents[1] / "fixtures" / "agent-execution-graph"
)
VALID_FIXTURES = sorted(FIXTURES.glob("valid-*.json"))


def _load_graph(path: Path) -> AgentExecutionGraph:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return AgentExecutionGraph.model_validate(doc)


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.stem)
def test_digest_reproduces_golden_fixtures(path: Path) -> None:
    """The spec canonicalisation recomputes each fixture's stored graph_digest."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    graph = AgentExecutionGraph.model_validate(doc)
    recomputed = compute_graph_digest(graph.capsule_id, graph.nodes, graph.edges)
    assert recomputed == doc["graph_digest"]


@pytest.mark.parametrize("path", VALID_FIXTURES, ids=lambda p: p.stem)
def test_model_roundtrips_fixture_documents(path: Path) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    graph = AgentExecutionGraph.model_validate(doc)
    assert graph.to_document() == doc


def test_stats_and_notes_are_excluded_from_digest() -> None:
    nodes = [
        GraphNode(id="a", kind="span", label="s", started_at=None, duration_ms=None)
    ]
    edges: list[GraphEdge] = []
    bare = AgentExecutionGraph.assemble("cap", nodes, edges)
    decorated = AgentExecutionGraph.assemble(
        "cap",
        nodes,
        edges,
        notes=[ReconstructionNote(kind="unlinked_span", node_id="a", detail="x")],
        stats=GraphStats(node_count=1, edge_count=0, max_depth=1, max_fan_out=0),
    )
    assert bare.graph_digest == decorated.graph_digest


def test_assemble_sorts_nodes_and_edges_canonically() -> None:
    nodes = [
        GraphNode(id="b", kind="span", label="b", started_at=None, duration_ms=None),
        GraphNode(id="a", kind="span", label="a", started_at=None, duration_ms=None),
    ]
    edges = [
        GraphEdge(type="span_parent", from_="b", to="a"),
        GraphEdge(type="follows", from_="a", to="b"),
    ]
    graph = AgentExecutionGraph.assemble("cap", nodes, edges)
    assert [n.id for n in graph.nodes] == ["a", "b"]
    assert [e.type for e in graph.edges] == ["follows", "span_parent"]
    # Input order never changes the digest.
    assert (
        AgentExecutionGraph.assemble("cap", list(reversed(nodes)), edges).graph_digest
        == graph.graph_digest
    )


def test_canonical_payload_excludes_optional_none_node_fields() -> None:
    node = GraphNode(
        id="a", kind="tool_call", label="t", started_at=None, duration_ms=None
    )
    payload = canonical_payload("cap", [node], [])
    assert payload["nodes"][0] == {
        "id": "a",
        "kind": "tool_call",
        "label": "t",
        "started_at": None,
        "duration_ms": None,
    }


def test_edge_serialises_with_from_keyword() -> None:
    edge = GraphEdge(type="follows", from_="a", to="b")
    assert edge.to_document() == {"type": "follows", "from": "a", "to": "b"}
    assert GraphEdge.model_validate({"type": "follows", "from": "a", "to": "b"}) == edge


def test_graph_digest_pattern_is_enforced() -> None:
    with pytest.raises(ValueError):
        AgentExecutionGraph(
            capsule_id="cap", nodes=[], edges=[], graph_digest="sha256:nothex"
        )
