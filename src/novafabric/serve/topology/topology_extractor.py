"""TopologyExtractor — maintains the server-side agent call graph.

Listens for agent lifecycle events, runs Louvain clustering when the edge
set changes by ≥ 10%, writes cluster layers to ClusterStore, and enqueues
TDP delta events on the DeltaBuffer.

Deviation from build prompt: FA2 layout uses networkx spring_layout as a
v0.1 approximation (FA2 is browser-side only in this Python implementation).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

import networkx as nx  # type: ignore[import-untyped]

from novafabric.serve.topology.cluster_store import ClusterStore
from novafabric.serve.topology.delta_buffer import DeltaBuffer

logger = logging.getLogger(__name__)

_LOUVAIN_TRIGGER_RATIO = 0.10  # 10% edge change triggers re-pass


class TopologyExtractor:
    """Server-side agent call graph with Louvain clustering.

    Thread-safe for single-writer / concurrent-reader use.
    Clustering is triggered asynchronously (does not block callers).
    """

    def __init__(
        self,
        cluster_store: ClusterStore,
        delta_buffer: DeltaBuffer,
        louvain_resolution: float = 1.0,
        collapse_isolated: bool = True,
    ) -> None:
        self._graph: nx.DiGraph = nx.DiGraph()
        self._cluster_store = cluster_store
        self._delta_buffer = delta_buffer
        self._resolution = louvain_resolution
        # Collapse degree-0 nodes (e.g. runs with no captured model calls) into a
        # single "misc" cluster instead of N singleton clusters — Louvain cannot
        # merge edgeless nodes, so without this they render as visual noise.
        self._collapse_isolated = collapse_isolated

        self._edges_at_last_pass = 0
        self._louvain_running = False

        # Optional: hook for testing — called after each Louvain pass
        self.on_louvain_complete: Callable[[], None] | None = None

    # ------------------------------------------------------------------
    # Graph mutations (synchronous — called from event bus handlers)
    # ------------------------------------------------------------------

    def add_agent(self, agent_id: str, agent_type: str = "unknown", status: str = "idle") -> None:
        if not self._graph.has_node(agent_id):
            self._graph.add_node(agent_id, agent_type=agent_type, status=status)
            self._delta_buffer.enqueue({
                "type": "add_node",
                "node_id": agent_id,
                "agent_type": agent_type,
                "status": status,
                "cluster_id": -1,
                "pos_x": 0.0,
                "pos_y": 0.0,
            })

    def update_agent_status(self, agent_id: str, status: str) -> None:
        if self._graph.has_node(agent_id):
            self._graph.nodes[agent_id]["status"] = status
            self._delta_buffer.enqueue({
                "type": "update_property",
                "node_id": agent_id,
                "key": "status",
                "value": status,
            })

    def add_edge(self, source: str, target: str, edge_type: str = "calls") -> None:
        if not self._graph.has_edge(source, target):
            self._graph.add_edge(source, target, edge_type=edge_type)
            self._delta_buffer.enqueue({
                "type": "add_edge",
                "source_id": source,
                "target_id": target,
                "edge_type": edge_type,
                "cluster_id": -1,
            })
            self._maybe_trigger_louvain()

    def node_count(self) -> int:
        return int(self._graph.number_of_nodes())

    def edge_count(self) -> int:
        return int(self._graph.number_of_edges())

    def get_edges(self) -> list[tuple[str, str]]:
        return list(self._graph.edges())

    def get_node_types(self) -> dict[str, str]:
        return {
            n: str(data.get("agent_type", "unknown"))
            for n, data in self._graph.nodes(data=True)
        }

    # ------------------------------------------------------------------
    # Louvain re-pass
    # ------------------------------------------------------------------

    def _maybe_trigger_louvain(self) -> None:
        if self._louvain_running:
            return
        current = self._graph.number_of_edges()
        if self._edges_at_last_pass == 0 and current > 0:
            self._schedule_louvain()
            return
        if self._edges_at_last_pass > 0:
            change_ratio = abs(current - self._edges_at_last_pass) / self._edges_at_last_pass
            if change_ratio >= _LOUVAIN_TRIGGER_RATIO:
                self._schedule_louvain()

    def _schedule_louvain(self) -> None:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.run_louvain_pass())
        except RuntimeError:
            # No running loop (tests or sync context) — run directly
            asyncio.run(self.run_louvain_pass())

    async def run_louvain_pass(self) -> None:
        if self._louvain_running:
            return
        self._louvain_running = True
        try:
            await asyncio.get_event_loop().run_in_executor(None, self._louvain_sync)
        finally:
            self._louvain_running = False
        if self.on_louvain_complete:
            self.on_louvain_complete()

    def _louvain_sync(self) -> None:
        """Run Louvain + layout and write cluster layer (blocking, runs in thread pool)."""
        if self._graph.number_of_nodes() == 0:
            return

        import community as community_louvain  # type: ignore[import-untyped]  # python-louvain

        # Louvain requires undirected graph
        ug = self._graph.to_undirected()
        try:
            partition = community_louvain.best_partition(
                ug, resolution=self._resolution, random_state=42
            )
        except Exception as exc:
            logger.warning("Louvain failed: %s — skipping cluster pass", exc)
            return

        # Collapse all degree-0 nodes into one shared "misc" cluster so the
        # dashboard shows a single super-node for unconnected runs rather than
        # one singleton per node.
        if self._collapse_isolated:
            degrees = dict(ug.degree())
            isolated = [n for n in ug.nodes() if degrees.get(n, 0) == 0]
            if isolated:
                connected_cids = [
                    partition[n] for n in ug.nodes() if degrees.get(n, 0) > 0
                ]
                misc_cid = (max(connected_cids) + 1) if connected_cids else 0
                for n in isolated:
                    partition[n] = misc_cid

        # Compute positions via spring layout (networkx — v0.1 FA2 approximation)
        try:
            pos = nx.spring_layout(ug, seed=42, k=1.5)
        except Exception:
            pos = {n: (0.0, 0.0) for n in ug.nodes()}

        # Build cluster metadata
        from collections import defaultdict
        cluster_nodes: dict[int, list[str]] = defaultdict(list)
        for node, cid in partition.items():
            cluster_nodes[cid].append(node)

        # Count inter-cluster edges
        inter_cluster: dict[int, int] = defaultdict(int)
        for u, v in self._graph.edges():
            cu, cv = partition.get(u, -1), partition.get(v, -1)
            if cu != cv:
                inter_cluster[cu] += 1
                inter_cluster[cv] += 1

        # Compute centroids
        cluster_rows: list[dict[str, object]] = []
        agent_nodes: list[dict[str, object]] = []
        for cid, nodes in cluster_nodes.items():
            xs = [pos[n][0] for n in nodes if n in pos]
            ys = [pos[n][1] for n in nodes if n in pos]
            cx = sum(xs) / len(xs) if xs else 0.0
            cy = sum(ys) / len(ys) if ys else 0.0
            cluster_rows.append({
                "cluster_id": cid,
                "centroid_x": cx,
                "centroid_y": cy,
                "agent_count": len(nodes),
                "inter_cluster_edges": inter_cluster.get(cid, 0),
            })
            for node in nodes:
                x, y = pos.get(node, (cx, cy))
                attrs = self._graph.nodes[node]
                agent_nodes.append({
                    "node_id": node,
                    "agent_type": attrs.get("agent_type", "unknown"),
                    "status": attrs.get("status", "idle"),
                    "cluster_id": cid,
                    "pos_x": float(x),
                    "pos_y": float(y),
                })

        # Write to ClusterStore (sync wrapper around async methods)
        # Build agent_edges
        agent_edge_rows: list[dict[str, object]] = []
        for u, v, data in self._graph.edges(data=True):
            cu = partition.get(u, -1)
            agent_edge_rows.append({
                "source_id": u,
                "target_id": v,
                "edge_type": data.get("edge_type", "calls"),
                "cluster_id": cu,
            })

        # Write synchronously via threading.Lock (safe from ThreadPoolExecutor).
        # asyncio.run() / run_coroutine_threadsafe both deadlock here because the
        # main event loop is blocked waiting for this executor task to finish.
        try:
            self._cluster_store.write_all_sync(cluster_rows, agent_nodes, agent_edge_rows)
        except Exception as exc:
            logger.warning("ClusterStore write failed: %s", exc)

        self._edges_at_last_pass = self._graph.number_of_edges()

        # Notify all clients: topology was reset
        self._delta_buffer.enqueue({"type": "topology_reset"})
        logger.info(
            "Louvain re-pass complete: %d clusters, %d nodes",
            len(cluster_rows),
            len(agent_nodes),
        )
