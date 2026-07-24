"""Synthesized graph-intelligence report (ADR-0215).

One read-only pass that turns the captured lineage graph into the answer to
"what does my graph say about my AI estate?": hubs and single points of
failure (reusing ADR-0212 verbatim), seeded Louvain communities, orphans,
health ratios, and best-effort cost hotspots. Every unavailable data source
is reported as unavailable — never fabricated (ADR-0215 I-2).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import networkx as nx  # type: ignore[import-untyped]

from novafabric.lineage.analytics._graph import build_nx_graph
from novafabric.lineage.analytics.centrality import (
    NodeMetric,
    compute_metrics_for_graph,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from novafabric.lineage._store import LineageStore

_NOTE = (
    "Descriptive synthesis of the captured lineage graph; rankings are for "
    "attention, not calibrated importance (ADR-0215)."
)
_COST_UNAVAILABLE_NOTE = "cost data unavailable (no cost records found)"
_COST_KEYS = ("cost_usd", "total_cost_usd", "cost_total", "cost")
_ORPHAN_CAP = 20
_COST_HOTSPOT_CAP = 5


@dataclass
class InsightsReport:
    node_counts_by_kind: dict[str, int]
    edge_counts_by_type: dict[str, int]
    top_hubs: list[NodeMetric]
    articulation_points: list[NodeMetric]
    communities: list[list[str]]
    orphan_nodes: list[str]
    orphan_total: int
    cost_hotspots: list[dict[str, Any]] | None
    cost_note: str
    health: dict[str, Any]
    sampled: bool
    note: str = _NOTE

    def as_dict(self) -> dict[str, Any]:
        return {
            "node_counts_by_kind": self.node_counts_by_kind,
            "edge_counts_by_type": self.edge_counts_by_type,
            "top_hubs": [m.as_dict() for m in self.top_hubs],
            "articulation_points": [m.as_dict() for m in self.articulation_points],
            "communities": self.communities,
            "community_count": len(self.communities),
            "orphan_nodes": self.orphan_nodes,
            "orphan_total": self.orphan_total,
            "cost_hotspots": self.cost_hotspots,
            "cost_note": self.cost_note,
            "health": self.health,
            "sampled": self.sampled,
            "note": self.note,
        }

    def to_markdown(self) -> str:
        lines = ["# NovaFabric graph insights", ""]
        health = ", ".join(f"{k}={v}" for k, v in sorted(self.health.items()))
        lines += [f"**Health:** {health}", ""]
        lines += ["## Nodes by kind", ""]
        for kind, count in sorted(self.node_counts_by_kind.items()):
            lines.append(f"- {kind}: {count}")
        lines += ["", "## Edges by type", ""]
        for edge_type, count in sorted(self.edge_counts_by_type.items()):
            lines.append(f"- {edge_type}: {count}")
        lines += ["", "## Top hubs", ""]
        if self.top_hubs:
            lines.append("| kind | ref | in | out | pagerank | SPOF |")
            lines.append("|---|---|---|---|---|---|")
            for m in self.top_hubs:
                spof = "yes" if m.is_articulation_point else ""
                lines.append(
                    f"| {m.kind} | {m.ref} | {m.degree_in} | {m.degree_out} "
                    f"| {m.pagerank:.4f} | {spof} |"
                )
        else:
            lines.append("(graph is empty)")
        lines += ["", "## Communities (Louvain, seeded)", ""]
        if self.communities:
            for i, members in enumerate(self.communities):
                lines.append(f"- community {i}: {len(members)} nodes — "
                             + ", ".join(members[:6])
                             + (" …" if len(members) > 6 else ""))
        else:
            lines.append("(none of size ≥ 2)")
        lines += ["", "## Orphans", ""]
        lines.append(
            f"{self.orphan_total} orphan node(s)"
            + (": " + ", ".join(self.orphan_nodes) if self.orphan_nodes else "")
        )
        lines += ["", "## Cost hotspots", ""]
        if self.cost_hotspots:
            for h in self.cost_hotspots:
                label = h.get("ref") or h.get("model", "?")
                cost = h.get("cost_usd")
                detail = (
                    f"{cost:.4f} USD" if isinstance(cost, float)
                    else f"{h.get('estimated_tokens', '?')} tokens"
                )
                lines.append(f"- {label}: {detail}")
        else:
            lines.append(f"({self.cost_note})")
        lines += ["", f"_{self.note}_", ""]
        return "\n".join(lines)


def _payload_cost(payload: dict[str, Any]) -> float | None:
    candidates: list[dict[str, Any]] = [payload]
    usage = payload.get("usage_totals")
    if isinstance(usage, dict):
        candidates.append(usage)
    for container in candidates:
        for key in _COST_KEYS:
            value = container.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None


def _cost_from_payloads(
    graph: nx.MultiDiGraph,
) -> tuple[list[dict[str, Any]] | None, str]:
    costed = []
    for node_id, data in graph.nodes(data=True):
        cost = _payload_cost(data.get("payload", {}))
        if cost is not None:
            costed.append(
                {"kind": data.get("kind", ""), "ref": data.get("ref", ""),
                 "cost_usd": cost}
            )
    if not costed:
        return None, _COST_UNAVAILABLE_NOTE
    costed.sort(key=lambda c: (-c["cost_usd"], c["ref"]))
    return costed[:_COST_HOTSPOT_CAP], "cost read from lineage node payloads"


def _cost_from_duckdb(path: Path) -> tuple[list[dict[str, Any]] | None, str]:
    """Best-effort per-model aggregate from an evidence-fabric accumulator DB."""
    try:
        import duckdb
    except ImportError:
        return None, f"cost source {path} ignored (duckdb extra not installed)"
    try:
        conn = duckdb.connect(str(path), read_only=True)
        rows = conn.execute(
            """
            SELECT model,
                   COUNT(*) AS calls,
                   COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(tokens_out), 0)
                       AS estimated_tokens
            FROM capsule_events
            WHERE event_type = 'model_call' AND model IS NOT NULL AND model != ''
            GROUP BY model
            ORDER BY estimated_tokens DESC, model
            LIMIT ?
            """,
            [_COST_HOTSPOT_CAP],
        ).fetchall()
        conn.close()
    except Exception as exc:  # noqa: BLE001 - best-effort source, degrade honestly
        return None, f"cost source {path} unreadable ({type(exc).__name__})"
    if not rows:
        return None, f"cost source {path} holds no model_call events"
    hotspots = [
        {"model": model, "calls": int(calls), "estimated_tokens": int(tokens)}
        for model, calls, tokens in rows
    ]
    return hotspots, f"cost aggregated from {path}"


def build_insights_report(
    store: LineageStore,
    *,
    top_n: int = 10,
    seed: int = 0,
    cost_db: Path | None = None,
) -> InsightsReport:
    """Build the synthesized report over the whole lineage graph (ADR-0215)."""
    graph = build_nx_graph(store)
    metrics = compute_metrics_for_graph(graph, top_n=top_n, seed=seed)

    node_counts: dict[str, int] = {}
    for _, data in graph.nodes(data=True):
        kind = str(data.get("kind", ""))
        node_counts[kind] = node_counts.get(kind, 0) + 1
    edge_counts: dict[str, int] = {}
    for _, _, data in graph.edges(data=True):
        edge_type = str(data.get("edge_type", ""))
        edge_counts[edge_type] = edge_counts.get(edge_type, 0) + 1

    def _label(node_id: str) -> str:
        data = graph.nodes[node_id]
        return f"{data.get('kind', '')}:{data.get('ref', '')}"

    communities: list[list[str]] = []
    if graph.number_of_nodes():
        undirected = nx.Graph(graph.to_undirected())
        for community in nx.community.louvain_communities(undirected, seed=seed):
            if len(community) < 2:
                continue
            communities.append(sorted(_label(n) for n in community))
        communities.sort(key=lambda members: (-len(members), members[0]))

    orphan_labels = sorted(
        _label(n) for n in graph.nodes if graph.degree(n) == 0
    )

    node_count = graph.number_of_nodes()
    if node_count:
        largest = max(
            (len(c) for c in nx.weakly_connected_components(graph)), default=0
        )
        largest_fraction = round(largest / node_count, 4)
        orphan_ratio = round(len(orphan_labels) / node_count, 4)
    else:
        largest_fraction = 0.0
        orphan_ratio = 0.0

    if cost_db is not None:
        cost_hotspots, cost_note = _cost_from_duckdb(cost_db)
    else:
        cost_hotspots, cost_note = _cost_from_payloads(graph)

    return InsightsReport(
        node_counts_by_kind=dict(sorted(node_counts.items())),
        edge_counts_by_type=dict(sorted(edge_counts.items())),
        top_hubs=metrics.top_hubs,
        articulation_points=metrics.articulation_points,
        communities=communities,
        orphan_nodes=orphan_labels[:_ORPHAN_CAP],
        orphan_total=len(orphan_labels),
        cost_hotspots=cost_hotspots,
        cost_note=cost_note,
        health={
            "node_count": node_count,
            "edge_count": graph.number_of_edges(),
            "largest_component_fraction": largest_fraction,
            "orphan_ratio": orphan_ratio,
        },
        sampled=metrics.sampled,
    )
