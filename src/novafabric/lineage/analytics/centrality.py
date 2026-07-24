"""Descriptive graph metrics over the lineage graph (ADR-0212).

Degree, PageRank, betweenness, and articulation points — the classical
"which node is my critical hub / single point of failure?" battery. Scores
are relative rankings for operator attention, **not** calibrated importance
(ADR-0212 D5). Read-only; deterministic (fixed seeds, canonical ordering).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import networkx as nx  # type: ignore[import-untyped]

from novafabric.lineage.analytics._graph import (
    build_nx_graph,
    collapse_to_weighted_digraph,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from novafabric.lineage._store import LineageStore

_RANKING_NOTE = (
    "Scores are relative rankings for attention, not calibrated importance "
    "(ADR-0212)."
)


def _pagerank(
    graph: nx.DiGraph,
    *,
    alpha: float = 0.85,
    max_iter: int = 100,
    tol: float = 1.0e-8,
) -> dict[str, float]:
    """Bounded, deterministic power-iteration PageRank over a weighted digraph.

    networkx 3.x's ``nx.pagerank`` requires scipy, which is not a NovaFabric
    dependency (ADR-0212 D2). Non-convergence within *max_iter* returns the
    best-effort ranking rather than raising — bounded by design.
    """
    nodes = sorted(graph.nodes())
    n = len(nodes)
    if n == 0:
        return {}
    rank = dict.fromkeys(nodes, 1.0 / n)
    out_weight = {
        u: float(sum(attrs.get("weight", 1) for attrs in graph[u].values()))
        for u in nodes
    }
    for _ in range(max_iter):
        prev = rank
        dangling = sum(prev[u] for u in nodes if out_weight[u] == 0.0)
        base = (1.0 - alpha) / n + alpha * dangling / n
        rank = dict.fromkeys(nodes, base)
        for u in nodes:
            if out_weight[u] == 0.0:
                continue
            share = alpha * prev[u] / out_weight[u]
            for v, attrs in graph[u].items():
                rank[v] += share * attrs.get("weight", 1)
        if sum(abs(rank[u] - prev[u]) for u in nodes) < n * tol:
            break
    return rank

#: Above this node count, betweenness switches to seeded k-sampling (ADR-0212 D4).
DEFAULT_BETWEENNESS_SAMPLE_THRESHOLD = 2000
_BETWEENNESS_SAMPLE_K = 256


@dataclass
class NodeMetric:
    node_id: str
    kind: str
    ref: str
    degree_in: int
    degree_out: int
    pagerank: float
    betweenness: float
    is_articulation_point: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": self.kind,
            "ref": self.ref,
            "degree_in": self.degree_in,
            "degree_out": self.degree_out,
            "pagerank": round(self.pagerank, 6),
            "betweenness": round(self.betweenness, 6),
            "is_articulation_point": self.is_articulation_point,
        }


@dataclass
class GraphMetricsReport:
    node_count: int
    edge_count: int
    top_hubs: list[NodeMetric]
    articulation_points: list[NodeMetric]
    sampled: bool
    note: str = _RANKING_NOTE

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "top_hubs": [m.as_dict() for m in self.top_hubs],
            "articulation_points": [m.as_dict() for m in self.articulation_points],
            "sampled": self.sampled,
            "note": self.note,
        }


def compute_graph_metrics(
    store: LineageStore,
    *,
    top_n: int = 20,
    betweenness_sample_threshold: int = DEFAULT_BETWEENNESS_SAMPLE_THRESHOLD,
    seed: int = 0,
) -> GraphMetricsReport:
    """Compute the metrics battery over the whole lineage graph.

    Empty graph → empty report, never an error. Raises
    :class:`~novafabric.lineage._store.LineageGraphTooLargeError` when the
    store exceeds its whole-graph bounds (ADR-0212 I-2).
    """
    graph = build_nx_graph(store)
    return compute_metrics_for_graph(
        graph,
        top_n=top_n,
        betweenness_sample_threshold=betweenness_sample_threshold,
        seed=seed,
    )


def compute_metrics_for_graph(
    graph: nx.MultiDiGraph,
    *,
    top_n: int = 20,
    betweenness_sample_threshold: int = DEFAULT_BETWEENNESS_SAMPLE_THRESHOLD,
    seed: int = 0,
) -> GraphMetricsReport:
    """Metrics over an already-built graph (lets ADR-0215 reuse one build)."""
    node_count = graph.number_of_nodes()
    edge_count = graph.number_of_edges()
    if node_count == 0:
        return GraphMetricsReport(0, 0, [], [], sampled=False)

    simple = collapse_to_weighted_digraph(graph)
    pagerank = _pagerank(simple)

    sampled = node_count > betweenness_sample_threshold
    if sampled:
        betweenness = nx.betweenness_centrality(
            simple, k=min(node_count, _BETWEENNESS_SAMPLE_K), seed=seed
        )
    else:
        betweenness = nx.betweenness_centrality(simple)

    articulation = set(nx.articulation_points(nx.Graph(simple.to_undirected())))

    metrics: dict[str, NodeMetric] = {}
    for node_id, data in graph.nodes(data=True):
        metrics[node_id] = NodeMetric(
            node_id=node_id,
            kind=str(data.get("kind", "")),
            ref=str(data.get("ref", "")),
            degree_in=graph.in_degree(node_id),
            degree_out=graph.out_degree(node_id),
            pagerank=float(pagerank.get(node_id, 0.0)),
            betweenness=float(betweenness.get(node_id, 0.0)),
            is_articulation_point=node_id in articulation,
        )

    # Hubs are ranked by connectivity first: raw PageRank would crown small
    # cycles (rank sinks) over the most-consumed asset.
    ranked = sorted(
        metrics.values(),
        key=lambda m: (-(m.degree_in + m.degree_out), -m.pagerank, m.node_id),
    )
    articulation_ranked = [m for m in ranked if m.is_articulation_point]
    return GraphMetricsReport(
        node_count=node_count,
        edge_count=edge_count,
        top_hubs=ranked[: max(top_n, 0)],
        articulation_points=articulation_ranked,
        sampled=sampled,
    )
