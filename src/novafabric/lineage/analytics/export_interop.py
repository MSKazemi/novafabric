"""Lineage graph export to GraphML / GEXF / Cypher (ADR-0214).

Pure functions over the canonically-ordered node/edge dicts from
``all_nodes``/``all_edges`` (or ``collect_subgraph``). Identical input →
byte-identical output, per the ADR-0124 export contract. These exports carry
topology and attributes only — never seal or signature material (ADR-0214
I-4).
"""
from __future__ import annotations

import json
import re
from typing import Any

import networkx as nx  # type: ignore[import-untyped]

#: networkx's GEXF writer stamps today's date; pinned for byte-stability (I-1).
_GEXF_PINNED_DATE = "1970-01-01"

_GEXF_DATE_RE = re.compile(r'lastmodifieddate="[^"]*"')
_CYPHER_REL_SANITISE_RE = re.compile(r"[^A-Z0-9_]")


def _build_export_graph(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> nx.MultiDiGraph:
    graph = nx.MultiDiGraph()
    for node in nodes:
        graph.add_node(node["node_id"], kind=node["kind"], ref=node["ref"])
    for edge in edges:
        attrs: dict[str, Any] = {
            "edge_type": edge["edge_type"],
            "capsule_run_id": edge["capsule_run_id"],
            "created_at": edge["created_at"],
        }
        if edge.get("confidence"):
            attrs["confidence"] = edge["confidence"]
        facets = edge.get("payload", {}).get("facets")
        if facets:
            # GraphML/GEXF have no nested attributes; a JSON string keeps the
            # facets machine-recoverable instead of silently dropped (D3).
            attrs["facets_json"] = json.dumps(facets, sort_keys=True)
        graph.add_edge(
            edge["source_id"], edge["target_id"], key=edge["edge_id"], **attrs
        )
    return graph


def to_graphml(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> str:
    """GraphML document (Gephi, yEd, igraph, networkx round-trip)."""
    graph = _build_export_graph(nodes, edges)
    return "\n".join(nx.generate_graphml(graph)) + "\n"


def to_gexf(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    """GEXF document (Gephi native)."""
    graph = _build_export_graph(nodes, edges)
    text = "\n".join(nx.generate_gexf(graph)) + "\n"
    return _GEXF_DATE_RE.sub(
        f'lastmodifieddate="{_GEXF_PINNED_DATE}"', text
    )


def _cypher_escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _cypher_rel_type(edge_type: str) -> str:
    rel = _CYPHER_REL_SANITISE_RE.sub("_", edge_type.upper())
    if not rel or not (rel[0].isalpha() or rel[0] == "_"):
        rel = f"E_{rel}"
    return rel


def to_cypher(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> str:
    """Idempotent Cypher ``MERGE`` statements for Neo4j-compatible stores.

    ``MERGE`` (not ``CREATE``) so re-importing the same export never
    duplicates the graph (ADR-0214 D2).
    """
    lines = [
        "// NovaFabric lineage export (ADR-0214). Idempotent: MERGE only.",
    ]
    for node in nodes:
        node_id = _cypher_escape(node["node_id"])
        kind = _cypher_escape(node["kind"])
        ref = _cypher_escape(node["ref"])
        lines.append(
            f'MERGE (n:Lineage {{node_id: "{node_id}"}}) '
            f'SET n.kind = "{kind}", n.ref = "{ref}";'
        )
    for edge in edges:
        src = _cypher_escape(edge["source_id"])
        tgt = _cypher_escape(edge["target_id"])
        edge_id = _cypher_escape(edge["edge_id"])
        rel = _cypher_rel_type(edge["edge_type"])
        sets = [
            f'r.edge_type = "{_cypher_escape(edge["edge_type"])}"',
            f'r.capsule_run_id = "{_cypher_escape(edge["capsule_run_id"])}"',
            f'r.created_at = "{_cypher_escape(edge["created_at"])}"',
        ]
        if edge.get("confidence"):
            sets.append(f'r.confidence = "{_cypher_escape(edge["confidence"])}"')
        set_clause = ", ".join(sets)
        lines.append(
            f'MATCH (a:Lineage {{node_id: "{src}"}}), '
            f'(b:Lineage {{node_id: "{tgt}"}}) '
            f'MERGE (a)-[r:{rel} {{edge_id: "{edge_id}"}}]->(b) '
            f"SET {set_clause};"
        )
    return "\n".join(lines) + "\n"
