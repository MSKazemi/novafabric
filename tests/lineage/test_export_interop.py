"""ADR-0214 — byte-stable GraphML/GEXF/Cypher export of the lineage graph.

Golden files live in ``tests/fixtures/lineage_export/``. Regenerate after an
intentional format change with::

    NOVA_REGEN_GOLDENS=1 uv run pytest tests/lineage/test_export_interop.py
"""
from __future__ import annotations

import os
from pathlib import Path

import networkx as nx  # type: ignore[import-untyped]

from novafabric.lineage.analytics._graph import collect_subgraph
from novafabric.lineage.analytics.export_interop import (
    to_cypher,
    to_gexf,
    to_graphml,
)

GOLDEN_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "lineage_export"


def _full(store) -> tuple[list, list]:
    return store.all_nodes(), store.all_edges()


def _golden(name: str, rendered: str) -> str:
    if os.environ.get("NOVA_REGEN_GOLDENS"):
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        (GOLDEN_DIR / name).write_text(rendered, encoding="utf-8")
    return (GOLDEN_DIR / name).read_text(encoding="utf-8")


def test_graphml_matches_golden_bytes(seeded_lineage_store):
    rendered = to_graphml(*_full(seeded_lineage_store))
    assert rendered == _golden("expected.graphml", rendered)


def test_cypher_matches_golden_bytes(seeded_lineage_store):
    rendered = to_cypher(*_full(seeded_lineage_store))
    assert rendered == _golden("expected.cypher", rendered)


def test_graphml_round_trips_via_networkx(seeded_lineage_store):
    nodes, edges = _full(seeded_lineage_store)
    parsed = nx.parse_graphml(to_graphml(nodes, edges))
    assert parsed.number_of_nodes() == len(nodes)
    assert parsed.number_of_edges() == len(edges)
    kinds = {data["kind"] for _, data in parsed.nodes(data=True)}
    assert kinds == {"run", "asset", "external"}
    edge_types = {
        data["edge_type"] for _, _, data in parsed.edges(data=True)
    }
    assert "consumed" in edge_types and "produced_by" in edge_types


def test_gexf_deterministic_with_pinned_date(seeded_lineage_store):
    nodes, edges = _full(seeded_lineage_store)
    first = to_gexf(nodes, edges)
    second = to_gexf(nodes, edges)
    assert first == second
    assert 'lastmodifieddate="1970-01-01"' in first


def test_cypher_escapes_hostile_refs():
    nodes = [
        {"node_id": "n1", "kind": "asset", "ref": 'evil"ref\\path'},
        {"node_id": "n2", "kind": "run", "ref": "run-x"},
    ]
    edges = [
        {
            "edge_id": "e1", "edge_type": "consumed?!", "source_id": "n2",
            "target_id": "n1", "capsule_run_id": "cap", "created_at": "t",
            "confidence": "observed", "payload": {},
        }
    ]
    rendered = to_cypher(nodes, edges)
    assert 'evil\\"ref\\\\path' in rendered
    assert "[r:CONSUMED__" in rendered  # sanitised relationship type
    assert "MERGE" in rendered and "CREATE" not in rendered.replace("MERGE", "")


def test_empty_graph_exports_are_valid():
    parsed = nx.parse_graphml(to_graphml([], []))
    assert parsed.number_of_nodes() == 0
    cypher = to_cypher([], [])
    assert cypher.startswith("//")
    assert not [ln for ln in cypher.splitlines() if ln.startswith(("MERGE", "MATCH"))]


def test_subgraph_scope_limits_export(seeded_lineage_store):
    nodes, edges = collect_subgraph(seeded_lineage_store, "run-victim", kind="run")
    refs = {n["ref"] for n in nodes}
    assert refs == {
        "run-victim", "local:stale-data@v3", "run-bad", "local:hub-model@v1"
    }
    assert len(edges) == 3
    # Facets survive into the export as a JSON attribute.
    rendered = to_graphml(nodes, edges)
    assert "facets_json" in rendered
