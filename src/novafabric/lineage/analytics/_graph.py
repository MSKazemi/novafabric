"""Build in-memory networkx graphs from the lineage store (ADR-0212).

Shared by centrality, root-cause, export, and insights so the whole cohort
pays for one graph build, not four.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import networkx as nx  # type: ignore[import-untyped]

if TYPE_CHECKING:  # pragma: no cover - typing only
    from novafabric.lineage._store import LineageStore


def build_nx_graph(
    store: LineageStore, *, edge_types: set[str] | None = None
) -> nx.MultiDiGraph:
    """The whole lineage graph as a ``MultiDiGraph`` (bounded by the store).

    Nodes carry ``kind``/``ref``/``payload``; edges carry ``edge_type``,
    ``confidence``, ``created_at``, ``capsule_run_id``, and the parsed edge
    ``payload``. Optional *edge_types* filters edges (nodes stay).
    """
    graph = nx.MultiDiGraph()
    for node in store.all_nodes():
        graph.add_node(
            node["node_id"],
            kind=node["kind"],
            ref=node["ref"],
            payload=node["payload"],
        )
    for edge in store.all_edges():
        if edge_types is not None and edge["edge_type"] not in edge_types:
            continue
        graph.add_edge(
            edge["source_id"],
            edge["target_id"],
            key=edge["edge_id"],
            edge_type=edge["edge_type"],
            confidence=edge.get("confidence"),
            created_at=edge["created_at"],
            capsule_run_id=edge["capsule_run_id"],
            payload=edge["payload"],
        )
    return graph


def collapse_to_weighted_digraph(graph: nx.MultiDiGraph) -> nx.DiGraph:
    """Collapse parallel edges to integer weights (ADR-0212 D5 caveat applies)."""
    simple = nx.DiGraph()
    simple.add_nodes_from(graph.nodes(data=False))
    for source, target in graph.edges(keys=False):
        if simple.has_edge(source, target):
            simple[source][target]["weight"] += 1
        else:
            simple.add_edge(source, target, weight=1)
    return simple


def collect_subgraph(
    store: LineageStore,
    ref: str,
    kind: str | None = None,
    depth: int = 5,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Node/edge dicts for the neighbourhood of *ref* (provenance ∪ blast radius).

    Returns ``([], [])`` when *ref* is unknown. Canonical ordering is inherited
    from ``all_nodes``/``all_edges``, so exports over a subgraph stay
    byte-stable (ADR-0214 I-1).
    """
    start = store._node_id_for_ref(ref, kind)
    if start is None:
        return [], []
    node_ids = {start}
    for row in store.provenance(ref, kind=kind, depth=depth):
        node_ids.add(row["node_id"])
    for row in store.blast_radius(ref, kind=kind, depth=depth):
        node_ids.add(row["node_id"])
    nodes = [n for n in store.all_nodes() if n["node_id"] in node_ids]
    edges = [
        e
        for e in store.all_edges()
        if e["source_id"] in node_ids and e["target_id"] in node_ids
    ]
    return nodes, edges
