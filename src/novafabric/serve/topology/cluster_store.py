"""ClusterStore — DuckDB in-process store for the topology cluster layer.

Holds the materialized Louvain cluster layer and per-cluster agent/edge data.
All writes are serialized via an asyncio.Lock to prevent concurrent write races.
"""

from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path
from typing import Any

import duckdb

from novafabric.serve.topology.ads_encoder import (
    encode_cluster_layer,
    encode_subgraph_edges,
    encode_subgraph_nodes,
)

_DDL = """
CREATE TABLE IF NOT EXISTS cluster_layer (
    cluster_id          BIGINT PRIMARY KEY,
    centroid_x          DOUBLE NOT NULL,
    centroid_y          DOUBLE NOT NULL,
    agent_count         BIGINT NOT NULL,
    inter_cluster_edges BIGINT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS agent_nodes (
    node_id     VARCHAR PRIMARY KEY,
    agent_type  VARCHAR NOT NULL DEFAULT 'unknown',
    status      VARCHAR NOT NULL DEFAULT 'idle',
    cluster_id  BIGINT  NOT NULL,
    pos_x       DOUBLE  NOT NULL DEFAULT 0.0,
    pos_y       DOUBLE  NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS agent_edges (
    source_id  VARCHAR NOT NULL,
    target_id  VARCHAR NOT NULL,
    edge_type  VARCHAR NOT NULL DEFAULT 'calls',
    cluster_id BIGINT  NOT NULL,
    PRIMARY KEY (source_id, target_id)
);
"""


def _default_db_path() -> Path:
    env = os.environ.get("NOVA_DASHBOARD_DUCKDB_PATH")
    if env:
        return Path(env)
    # Derive from nova_home() (NOVAFABRIC_HOME-aware) — a hardcoded
    # Path.home()/.novafabric here bypassed the _paths.py contract, so custom-home
    # deployments (and hermetic tests) all locked one shared dashboard.duckdb.
    from novafabric._paths import nova_home

    return nova_home() / "dashboard.duckdb"


class ClusterStore:
    """DuckDB-backed store for the topology cluster layer and per-cluster data."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or _default_db_path()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(str(self._db_path))
        self._conn.execute("BEGIN")
        self._conn.execute(_DDL)
        self._conn.execute("COMMIT")
        self._lock = asyncio.Lock()
        # Used by _louvain_sync (runs in ThreadPoolExecutor — no asyncio loop available)
        self._thread_lock = threading.Lock()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------
    # Synchronous writes — called from ThreadPoolExecutor (_louvain_sync).
    # Uses threading.Lock; DuckDB connection is shared but protected.
    # ------------------------------------------------------------------

    def write_all_sync(
        self,
        clusters: list[dict[str, Any]],
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> None:
        with self._thread_lock:
            self._conn.execute("DELETE FROM cluster_layer")
            for c in clusters:
                self._conn.execute(
                    "INSERT OR REPLACE INTO cluster_layer VALUES (?, ?, ?, ?, ?)",
                    [c["cluster_id"], c["centroid_x"], c["centroid_y"],
                     c["agent_count"], c["inter_cluster_edges"]],
                )
            self._conn.execute("DELETE FROM agent_nodes")
            for n in nodes:
                self._conn.execute(
                    "INSERT OR REPLACE INTO agent_nodes VALUES (?, ?, ?, ?, ?, ?)",
                    [n["node_id"], n.get("agent_type", "unknown"), n.get("status", "idle"),
                     n["cluster_id"], n.get("pos_x", 0.0), n.get("pos_y", 0.0)],
                )
            self._conn.execute("DELETE FROM agent_edges")
            for e in edges:
                self._conn.execute(
                    "INSERT OR REPLACE INTO agent_edges VALUES (?, ?, ?, ?)",
                    [e["source_id"], e["target_id"], e.get("edge_type", "calls"), e["cluster_id"]],
                )

    # ------------------------------------------------------------------
    # Writes (async-serialised)
    # ------------------------------------------------------------------

    async def write_cluster_layer(self, clusters: list[dict[str, Any]]) -> None:
        async with self._lock:
            self._conn.execute("DELETE FROM cluster_layer")
            if clusters:
                for c in clusters:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO cluster_layer VALUES (?, ?, ?, ?, ?)",
                        [c["cluster_id"], c["centroid_x"], c["centroid_y"],
                         c["agent_count"], c["inter_cluster_edges"]],
                    )

    async def write_agent_nodes(self, nodes: list[dict[str, Any]]) -> None:
        async with self._lock:
            for n in nodes:
                self._conn.execute(
                    "INSERT OR REPLACE INTO agent_nodes VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        n["node_id"],
                        n.get("agent_type", "unknown"),
                        n.get("status", "idle"),
                        n["cluster_id"],
                        n.get("pos_x", 0.0),
                        n.get("pos_y", 0.0),
                    ],
                )

    async def write_agent_edges(self, edges: list[dict[str, Any]]) -> None:
        async with self._lock:
            for e in edges:
                self._conn.execute(
                    "INSERT OR REPLACE INTO agent_edges VALUES (?, ?, ?, ?)",
                    [e["source_id"], e["target_id"], e.get("edge_type", "calls"), e["cluster_id"]],
                )

    # ------------------------------------------------------------------
    # Reads (Arrow IPC)
    # ------------------------------------------------------------------

    def fetch_cluster_layer_arrow(self) -> bytes:
        rows = self._conn.execute(
            "SELECT cluster_id, centroid_x, centroid_y, agent_count, inter_cluster_edges "
            "FROM cluster_layer ORDER BY cluster_id"
        ).fetchall()
        dicts = [
            {
                "cluster_id": r[0],
                "centroid_x": r[1],
                "centroid_y": r[2],
                "agent_count": r[3],
                "inter_cluster_edges": r[4],
            }
            for r in rows
        ]
        return encode_cluster_layer(dicts)

    def fetch_subgraph_arrow(self, cluster_id: int) -> tuple[bytes, bytes]:
        node_rows = self._conn.execute(
            "SELECT node_id, agent_type, status, cluster_id, pos_x, pos_y "
            "FROM agent_nodes WHERE cluster_id = ?",
            [cluster_id],
        ).fetchall()
        edge_rows = self._conn.execute(
            "SELECT source_id, target_id, edge_type, cluster_id "
            "FROM agent_edges WHERE cluster_id = ?",
            [cluster_id],
        ).fetchall()

        node_dicts = [
            {"node_id": r[0], "agent_type": r[1], "status": r[2],
             "cluster_id": r[3], "pos_x": r[4], "pos_y": r[5]}
            for r in node_rows
        ]
        edge_dicts = [
            {"source_id": r[0], "target_id": r[1], "edge_type": r[2], "cluster_id": r[3]}
            for r in edge_rows
        ]
        return encode_subgraph_nodes(node_dicts), encode_subgraph_edges(edge_dicts)

    def fetch_inter_cluster_edges(self) -> list[dict[str, Any]]:
        """Return cross-cluster edge aggregates as {source_cluster, target_cluster, count}."""
        rows = self._conn.execute("""
            SELECT
                src.cluster_id AS source_cluster,
                tgt.cluster_id AS target_cluster,
                COUNT(*) AS edge_count
            FROM agent_edges e
            JOIN agent_nodes src ON e.source_id = src.node_id
            JOIN agent_nodes tgt ON e.target_id = tgt.node_id
            WHERE src.cluster_id != tgt.cluster_id
            GROUP BY src.cluster_id, tgt.cluster_id
        """).fetchall()
        return [
            {"source_cluster": int(r[0]), "target_cluster": int(r[1]), "count": int(r[2])}
            for r in rows
        ]

    def cluster_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM cluster_layer").fetchone()
        return int(row[0]) if row else 0

    def list_clusters(self) -> list[dict[str, Any]]:
        """Return clusters as plain JSON rows for the dashboard Table/Treemap views.

        Ordered largest-first so the most significant clusters surface at the top.
        """
        rows = self._conn.execute(
            "SELECT cluster_id, agent_count, inter_cluster_edges "
            "FROM cluster_layer ORDER BY agent_count DESC, cluster_id ASC"
        ).fetchall()
        return [
            {
                "cluster_id": int(r[0]),
                "agent_count": int(r[1]),
                "inter_cluster_edges": int(r[2]),
            }
            for r in rows
        ]
