"""Read-only graph-analytics layer over the lineage store (ADR-0212).

Derived, rebuildable computation only — no state, no schema, no writes. The
durable :class:`~novafabric.lineage._store.LineageStore` stays the single
source of truth (the ADR-0083 precedent).
"""
from novafabric.lineage.analytics.centrality import (
    GraphMetricsReport,
    NodeMetric,
    compute_graph_metrics,
)
from novafabric.lineage.analytics.export_interop import (
    to_cypher,
    to_gexf,
    to_graphml,
)
from novafabric.lineage.analytics.insights import (
    InsightsReport,
    build_insights_report,
)
from novafabric.lineage.analytics.root_cause import (
    RootCauseReport,
    SuspectNode,
    UnknownLineageRunError,
    rank_root_causes,
)

__all__ = [
    "GraphMetricsReport",
    "InsightsReport",
    "NodeMetric",
    "RootCauseReport",
    "SuspectNode",
    "UnknownLineageRunError",
    "build_insights_report",
    "compute_graph_metrics",
    "rank_root_causes",
    "to_cypher",
    "to_gexf",
    "to_graphml",
]
