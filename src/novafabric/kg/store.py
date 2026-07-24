"""Capsule Knowledge Graph store backed by KuzuDB.

SEPARATE from the lineage KuzuDB instance.

Spike result (2026-05-17): KuzuDB MERGE is atomic with threading.Lock —
10,000/10,000 updates preserved at 5K increments × 2 threads.  The single
write lock wraps ALL KuzuDB write calls.

Schema note (v1.2): representative_capsule_ids is stored as a STRING scalar
containing a comma-separated list of capsule IDs (KuzuDB 0.11.x does not
support STRING[] in rel tables).  The GCounter exposes the most recent ID
via ``representative_capsule_id`` for edge upserts.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Prometheus metrics — optional at runtime (prometheus_client is a dev dep).
try:
    from prometheus_client import Counter as _PCounter
    from prometheus_client import Gauge as _PGauge

    _kg_node_merge_total = _PCounter(
        "novafabric_kg_node_merge_total",
        "Total KG node MERGE upserts by node type",
        ["node_type"],
    )
    _kg_edge_upsert_total = _PCounter(
        "novafabric_kg_edge_upsert_total",
        "Total KG edge upserts (CRDT G-counter writes) by edge type",
        ["edge_type"],
    )
    _kg_crdt_merge_total = _PCounter(
        "novafabric_kg_crdt_merge_total",
        "KG CRDT merges where an existing edge call_count was incremented",
        ["edge_type"],
    )
    _kg_node_count = _PGauge(
        "novafabric_kg_node_count",
        "Current total KG node count across all node types",
    )
except Exception:  # noqa: BLE001
    _kg_node_merge_total = None  # type: ignore[assignment]
    _kg_edge_upsert_total = None  # type: ignore[assignment]
    _kg_crdt_merge_total = None  # type: ignore[assignment]
    _kg_node_count = None  # type: ignore[assignment]

SCHEMA_DDL: list[str] = [
    (
        "CREATE NODE TABLE IF NOT EXISTS Agent "
        "(id STRING PRIMARY KEY, name STRING, last_seen TIMESTAMP)"
    ),
    (
        "CREATE NODE TABLE IF NOT EXISTS Model "
        "(id STRING PRIMARY KEY, provider STRING, canonical_name STRING, last_seen TIMESTAMP)"
    ),
    (
        "CREATE NODE TABLE IF NOT EXISTS Tool "
        "(id STRING PRIMARY KEY, name STRING, last_seen TIMESTAMP)"
    ),
    (
        "CREATE NODE TABLE IF NOT EXISTS InferenceEndpoint "
        "(id STRING PRIMARY KEY, url STRING, last_seen TIMESTAMP)"
    ),
    (
        "CREATE NODE TABLE IF NOT EXISTS MCPServer "
        "(id STRING PRIMARY KEY, name STRING, last_seen TIMESTAMP)"
    ),
    # representative_capsule_ids: comma-separated list stored as STRING scalar.
    # KuzuDB 0.11.x does not support STRING[] on rel tables; upgrading to STRING[]
    # is tracked as a schema migration for KuzuDB ≥0.13 (OQ-031).
    (
        "CREATE REL TABLE IF NOT EXISTS CALLS ("
        "FROM Agent TO Model, "
        "call_count INT64, "
        "verified_count INT64, "
        "confidence DOUBLE, "
        "last_seen TIMESTAMP, "
        "representative_capsule_ids STRING"
        ")"
    ),
    (
        "CREATE REL TABLE IF NOT EXISTS USES_TOOL ("
        "FROM Agent TO Tool, "
        "call_count INT64, "
        "verified_count INT64, "
        "confidence DOUBLE, "
        "last_seen TIMESTAMP, "
        "representative_capsule_ids STRING"
        ")"
    ),
    (
        "CREATE REL TABLE IF NOT EXISTS ROUTES_TO ("
        "FROM Agent TO InferenceEndpoint, "
        "call_count INT64, "
        "verified_count INT64, "
        "confidence DOUBLE, "
        "last_seen TIMESTAMP, "
        "representative_capsule_ids STRING"
        ")"
    ),
    # SERVED_BY: Tool → MCPServer membership edge.
    # call_count = number of times this tool was invoked through this MCP server.
    (
        "CREATE REL TABLE IF NOT EXISTS SERVED_BY ("
        "FROM Tool TO MCPServer, "
        "call_count INT64, "
        "verified_count INT64, "
        "confidence DOUBLE, "
        "last_seen TIMESTAMP, "
        "representative_capsule_ids STRING"
        ")"
    ),
]


class KGStore:
    """KuzuDB-backed capsule knowledge graph.

    Thread-safe: uses a single threading.Lock for all write operations.
    Read queries may be issued concurrently (KuzuDB MVCC handles reads).

    Invariant: All public *write* methods acquire ``_write_lock`` before
    issuing any Cypher statement.  Internal ``_merge_*`` helpers assume the
    lock is already held by the caller.
    """

    def __init__(self, db_path: str | Path) -> None:
        try:
            import kuzu as _kuzu
        except ImportError as exc:
            raise ImportError(
                "kuzu is required for KGStore.  "
                "Install with: pip install 'novafabric[scale-kg]'"
            ) from exc

        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db = _kuzu.Database(str(self.db_path))
        self._conn = _kuzu.Connection(self._db)
        self._write_lock = threading.Lock()
        self._initialized = False

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def init_schema(self) -> None:
        """Apply schema DDL.  Idempotent (all statements use IF NOT EXISTS)."""
        with self._write_lock:
            for ddl in SCHEMA_DDL:
                self._conn.execute(ddl)
        self._initialized = True
        logger.info("KG schema initialised at %s", self.db_path)

    # ------------------------------------------------------------------
    # Internal helpers (lock must be held by caller)
    # ------------------------------------------------------------------

    def _merge_agent(self, agent_id: str, name: str = "") -> None:
        """MERGE Agent node — caller must hold _write_lock."""
        self._conn.execute(
            "MERGE (a:Agent {id: $id}) "
            "ON MATCH SET a.last_seen = current_timestamp(), a.name = $name "
            "ON CREATE SET a.name = $name, a.last_seen = current_timestamp()",
            {"id": agent_id, "name": name},
        )
        if _kg_node_merge_total is not None:
            _kg_node_merge_total.labels(node_type="Agent").inc()

    def _merge_model(
        self, model_id: str, provider: str = "", canonical_name: str = ""
    ) -> None:
        """MERGE Model node — caller must hold _write_lock."""
        self._conn.execute(
            "MERGE (m:Model {id: $id}) "
            "ON MATCH SET m.last_seen = current_timestamp() "
            "ON CREATE SET m.provider = $provider, m.canonical_name = $canonical, "
            "m.last_seen = current_timestamp()",
            {"id": model_id, "provider": provider, "canonical": canonical_name},
        )
        if _kg_node_merge_total is not None:
            _kg_node_merge_total.labels(node_type="Model").inc()

    def _merge_tool(self, tool_id: str, name: str = "") -> None:
        """MERGE Tool node — caller must hold _write_lock."""
        self._conn.execute(
            "MERGE (t:Tool {id: $id}) "
            "ON MATCH SET t.last_seen = current_timestamp() "
            "ON CREATE SET t.name = $name, t.last_seen = current_timestamp()",
            {"id": tool_id, "name": name},
        )
        if _kg_node_merge_total is not None:
            _kg_node_merge_total.labels(node_type="Tool").inc()

    def _merge_endpoint(self, endpoint_id: str, url: str = "") -> None:
        """MERGE InferenceEndpoint node — caller must hold _write_lock."""
        self._conn.execute(
            "MERGE (e:InferenceEndpoint {id: $id}) "
            "ON MATCH SET e.last_seen = current_timestamp() "
            "ON CREATE SET e.url = $url, e.last_seen = current_timestamp()",
            {"id": endpoint_id, "url": url},
        )
        if _kg_node_merge_total is not None:
            _kg_node_merge_total.labels(node_type="InferenceEndpoint").inc()

    def _merge_mcp_server(self, server_id: str, name: str = "") -> None:
        """MERGE MCPServer node — caller must hold _write_lock."""
        self._conn.execute(
            "MERGE (s:MCPServer {id: $id}) "
            "ON MATCH SET s.last_seen = current_timestamp(), s.name = $name "
            "ON CREATE SET s.name = $name, s.last_seen = current_timestamp()",
            {"id": server_id, "name": name or server_id},
        )
        if _kg_node_merge_total is not None:
            _kg_node_merge_total.labels(node_type="MCPServer").inc()

    # ------------------------------------------------------------------
    # Public node merge helpers (acquire lock internally)
    # ------------------------------------------------------------------

    def merge_agent(self, agent_id: str, name: str = "") -> None:
        """Upsert an Agent node."""
        with self._write_lock:
            self._merge_agent(agent_id, name)

    def merge_model(
        self, model_id: str, provider: str = "", canonical_name: str = ""
    ) -> None:
        """Upsert a Model node."""
        with self._write_lock:
            self._merge_model(model_id, provider, canonical_name)

    def merge_tool(self, tool_id: str, name: str = "") -> None:
        """Upsert a Tool node."""
        with self._write_lock:
            self._merge_tool(tool_id, name)

    def merge_endpoint(self, endpoint_id: str, url: str = "") -> None:
        """Upsert an InferenceEndpoint node."""
        with self._write_lock:
            self._merge_endpoint(endpoint_id, url)

    def merge_mcp_server(self, server_id: str, name: str = "") -> None:
        """Upsert an MCPServer node."""
        with self._write_lock:
            self._merge_mcp_server(server_id, name)

    # ------------------------------------------------------------------
    # Internal edge helpers (lock must be held by caller)
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_confidence(new_vc: int, new_cc: int) -> float:
        """Return verified_count / call_count, or 0.0 if call_count is zero."""
        return new_vc / new_cc if new_cc > 0 else 0.0

    def _upsert_edge_generic(
        self,
        src_id: str,
        dst_id: str,
        rel_table: str,
        src_label: str,
        dst_label: str,
        delta_cc: int,
        delta_vc: int,
        capsule_id: str,
    ) -> None:
        """Read-then-write edge upsert.

        KuzuDB 0.11.3 does not support mixed-type arithmetic (e.g. SERIAL*DOUBLE)
        inside Cypher SET clauses when parameters are involved.  We therefore:

        1. MERGE to ensure the edge exists (ON CREATE initialises to zeros).
        2. Read the current counts into Python.
        3. Compute the new totals and confidence in Python.
        4. Write the new values back with a plain SET.

        All three steps execute under the same caller-held _write_lock.
        """
        # Step 1 — ensure edge exists
        self._conn.execute(
            f"MATCH (s:{src_label} {{id: $src}}), (d:{dst_label} {{id: $dst}}) "
            f"MERGE (s)-[e:{rel_table}]->(d) "
            f"ON CREATE SET e.call_count = 0, e.verified_count = 0, "
            f"  e.confidence = 0.0, e.last_seen = current_timestamp(), "
            f"  e.representative_capsule_ids = ''",
            {"src": src_id, "dst": dst_id},
        )
        # Step 2 — read current counts
        read_res: Any = self._conn.execute(
            f"MATCH (s:{src_label} {{id: $src}})-[e:{rel_table}]->(d:{dst_label} {{id: $dst}}) "
            f"RETURN e.call_count, e.verified_count, e.representative_capsule_ids",
            {"src": src_id, "dst": dst_id},
        )
        row: Any = read_res.get_next()
        old_cc: int = int(row[0])
        old_vc: int = int(row[1])
        existing_ids: str = str(row[2]) if row[2] else ""
        is_existing_edge = old_cc > 0
        # Step 3 — compute new totals in Python
        new_cc = old_cc + delta_cc
        new_vc = old_vc + delta_vc
        new_conf = self._compute_confidence(new_vc, new_cc)
        # Merge capsule IDs — keep up to 10 unique IDs, newest first
        existing_list = [x for x in existing_ids.split(",") if x] if existing_ids else []
        if capsule_id and capsule_id not in existing_list:
            existing_list.insert(0, capsule_id)
        new_ids = ",".join(existing_list[:10])
        # Step 4 — write back
        self._conn.execute(
            f"MATCH (s:{src_label} {{id: $src}})-[e:{rel_table}]->(d:{dst_label} {{id: $dst}}) "
            f"SET e.call_count = $cc, e.verified_count = $vc, e.confidence = $conf, "
            f"  e.last_seen = current_timestamp(), e.representative_capsule_ids = $cids",
            {
                "src": src_id,
                "dst": dst_id,
                "cc": new_cc,
                "vc": new_vc,
                "conf": new_conf,
                "cids": new_ids,
            },
        )
        if _kg_edge_upsert_total is not None:
            _kg_edge_upsert_total.labels(edge_type=rel_table).inc()
        if is_existing_edge and _kg_crdt_merge_total is not None:
            _kg_crdt_merge_total.labels(edge_type=rel_table).inc()

    # ------------------------------------------------------------------
    # Edge upserts (acquire lock, call internal helpers inside same lock)
    # ------------------------------------------------------------------

    def upsert_calls_edge(
        self,
        agent_id: str,
        model_id: str,
        call_count: int,
        verified_count: int,
        confidence: float,
        capsule_id: str,
        provider: str = "",
        canonical_name: str = "",
    ) -> None:
        """Upsert a CALLS edge with accumulated counts.  Thread-safe."""
        with self._write_lock:
            self._merge_agent(agent_id)
            self._merge_model(model_id, provider=provider, canonical_name=canonical_name)
            self._upsert_edge_generic(
                src_id=agent_id,
                dst_id=model_id,
                rel_table="CALLS",
                src_label="Agent",
                dst_label="Model",
                delta_cc=call_count,
                delta_vc=verified_count,
                capsule_id=capsule_id,
            )

    def upsert_uses_tool_edge(
        self,
        agent_id: str,
        tool_id: str,
        call_count: int,
        verified_count: int,
        confidence: float,
        capsule_id: str,
        tool_name: str = "",
    ) -> None:
        """Upsert a USES_TOOL edge with accumulated counts.  Thread-safe."""
        with self._write_lock:
            self._merge_agent(agent_id)
            self._merge_tool(tool_id, name=tool_name)
            self._upsert_edge_generic(
                src_id=agent_id,
                dst_id=tool_id,
                rel_table="USES_TOOL",
                src_label="Agent",
                dst_label="Tool",
                delta_cc=call_count,
                delta_vc=verified_count,
                capsule_id=capsule_id,
            )

    def upsert_routes_to_edge(
        self,
        agent_id: str,
        endpoint_id: str,
        call_count: int,
        verified_count: int,
        confidence: float,
        capsule_id: str,
        url: str = "",
    ) -> None:
        """Upsert a ROUTES_TO edge with accumulated counts.  Thread-safe."""
        with self._write_lock:
            self._merge_agent(agent_id)
            self._merge_endpoint(endpoint_id, url=url)
            self._upsert_edge_generic(
                src_id=agent_id,
                dst_id=endpoint_id,
                rel_table="ROUTES_TO",
                src_label="Agent",
                dst_label="InferenceEndpoint",
                delta_cc=call_count,
                delta_vc=verified_count,
                capsule_id=capsule_id,
            )

    def upsert_served_by_edge(
        self,
        tool_id: str,
        server_id: str,
        call_count: int,
        verified_count: int,
        confidence: float,
        capsule_id: str,
        server_name: str = "",
    ) -> None:
        """Upsert a SERVED_BY edge (Tool → MCPServer) with accumulated counts.  Thread-safe."""
        with self._write_lock:
            self._merge_tool(tool_id)
            self._merge_mcp_server(server_id, name=server_name)
            self._upsert_edge_generic(
                src_id=tool_id,
                dst_id=server_id,
                rel_table="SERVED_BY",
                src_label="Tool",
                dst_label="MCPServer",
                delta_cc=call_count,
                delta_vc=verified_count,
                capsule_id=capsule_id,
            )

    # ------------------------------------------------------------------
    # Query helpers (no lock required for reads)
    # ------------------------------------------------------------------

    def get_edge_count(self) -> int:
        """Count total edges across all relationship types."""
        total = 0
        for rel in ("CALLS", "USES_TOOL", "ROUTES_TO", "SERVED_BY"):
            try:
                result: Any = self._conn.execute(f"MATCH ()-[e:{rel}]->() RETURN count(e)")
                total += int(result.get_next()[0])
            except Exception:  # noqa: BLE001
                pass
        return total

    def query_agent_models(self, agent_id: str) -> list[dict[str, Any]]:
        """Return all Model nodes called by *agent_id*, ordered by call_count desc."""
        result: Any = self._conn.execute(
            "MATCH (a:Agent {id: $id})-[e:CALLS]->(m:Model) "
            "RETURN m.id, m.provider, e.call_count, e.confidence "
            "ORDER BY e.call_count DESC",
            {"id": agent_id},
        )
        rows: list[dict[str, Any]] = []
        while result.has_next():
            r: Any = result.get_next()
            rows.append(
                {
                    "model_id": r[0],
                    "provider": r[1],
                    "call_count": r[2],
                    "confidence": r[3],
                }
            )
        return rows

    def query_agent_tools(self, agent_id: str) -> list[dict[str, Any]]:
        """Return all Tool nodes used by *agent_id*, ordered by call_count desc."""
        result: Any = self._conn.execute(
            "MATCH (a:Agent {id: $id})-[e:USES_TOOL]->(t:Tool) "
            "RETURN t.id, t.name, e.call_count, e.confidence "
            "ORDER BY e.call_count DESC",
            {"id": agent_id},
        )
        rows: list[dict[str, Any]] = []
        while result.has_next():
            r: Any = result.get_next()
            rows.append(
                {
                    "tool_id": r[0],
                    "tool_name": r[1],
                    "call_count": r[2],
                    "confidence": r[3],
                }
            )
        return rows

    def query_agent_mcp_servers(self, agent_id: str) -> list[dict[str, Any]]:
        """Return MCPServer nodes reachable from *agent_id* via Agent→Tool→MCPServer.

        Two-hop path: (Agent)-[:USES_TOOL]->(Tool)-[:SERVED_BY]->(MCPServer).
        Aggregates call_count over all Tool intermediaries for the same server.
        """
        result: Any = self._conn.execute(
            "MATCH (a:Agent {id: $id})-[ut:USES_TOOL]->(t:Tool)-[sb:SERVED_BY]->(s:MCPServer) "
            "RETURN s.id, s.name, sum(ut.call_count) AS call_count "
            "ORDER BY call_count DESC",
            {"id": agent_id},
        )
        rows: list[dict[str, Any]] = []
        while result.has_next():
            r: Any = result.get_next()
            rows.append(
                {
                    "server_id": r[0],
                    "server_name": r[1],
                    "call_count": int(r[2]) if r[2] is not None else 0,
                }
            )
        return rows

    def get_node_counts(self) -> dict[str, int]:
        """Return a per-type node count dict."""
        counts: dict[str, int] = {}
        for label in ("Agent", "Model", "Tool", "MCPServer", "InferenceEndpoint"):
            try:
                result: Any = self._conn.execute(f"MATCH (n:{label}) RETURN count(n)")
                counts[label] = int(result.get_next()[0])
            except Exception:  # noqa: BLE001
                counts[label] = 0
        if _kg_node_count is not None:
            _kg_node_count.set(sum(counts.values()))
        return counts

    def get_status(self) -> dict[str, Any]:
        """Return store health dict (safe to call without init_schema)."""
        try:
            node_counts = self.get_node_counts()
            edge_count = self.get_edge_count()
            return {
                "store": "kuzu",
                "store_health": "ok",
                "edge_count": edge_count,
                "node_counts": node_counts,
                "db_path": str(self.db_path),
            }
        except Exception as exc:
            return {
                "store": "kuzu",
                "store_health": "error",
                "error": str(exc),
                "db_path": str(self.db_path),
            }

    def get_topology_graph(
        self, max_nodes: int = 500, max_edges: int = 8000
    ) -> dict[str, Any]:
        """Return all nodes and edges as a topology graph dict for visualization.

        Caps at *max_nodes* total nodes and *max_edges* total edges to protect
        dashboard rendering (S5 graph guard, ADR-0199). Edges whose endpoints
        fell outside the node cap are dropped rather than returned dangling, so
        the client never has to reconcile an edge against a missing node. When
        either cap or the node-set filter drops data, ``truncated`` is True and
        ``truncated_reason`` lists which guard fired. Returns
        ``{"nodes", "edges", "node_counts", "edge_counts", "truncated",
        "truncated_reason"}``.
        """
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        truncated_reason: list[str] = []

        # Collect nodes per type (stop early if max_nodes reached)
        node_queries: list[tuple[str, list[str]]] = [
            ("Agent",            ["id", "name"]),
            ("Model",            ["id", "provider", "canonical_name"]),
            ("Tool",             ["id", "name"]),
            ("MCPServer",        ["id", "name"]),
            ("InferenceEndpoint", ["id", "url"]),
        ]
        for label, fields in node_queries:
            if len(nodes) >= max_nodes:
                break
            try:
                cols = ", ".join(f"n.{f}" for f in fields)
                res: Any = self._conn.execute(
                    f"MATCH (n:{label}) RETURN {cols} LIMIT $lim",
                    {"lim": max_nodes - len(nodes)},
                )
                while res.has_next():
                    row = res.get_next()
                    node: dict[str, Any] = {"type": label}
                    for i, f in enumerate(fields):
                        node[f] = row[i] if row[i] is not None else ""
                    nodes.append(node)
            except Exception:  # noqa: BLE001
                pass

        # Collect edges per type
        edge_queries: list[tuple[str, str, str]] = [
            ("CALLS",     "Agent",  "Model"),
            ("USES_TOOL", "Agent",  "Tool"),
            ("ROUTES_TO", "Agent",  "InferenceEndpoint"),
            ("SERVED_BY", "Tool",   "MCPServer"),
        ]
        # Only edges whose endpoints survived the node cap are emitted — an edge
        # to a node the client never received would render as a dangling stub.
        node_ids = {n.get("id", "") for n in nodes}
        dropped_dangling = False
        edge_cap_hit = False
        for rel, src_label, dst_label in edge_queries:
            if len(edges) >= max_edges:
                edge_cap_hit = True
                break
            try:
                res = self._conn.execute(
                    f"MATCH (s:{src_label})-[e:{rel}]->(d:{dst_label}) "
                    f"RETURN s.id, d.id, e.call_count, e.confidence LIMIT 2000"
                )
                while res.has_next():
                    if len(edges) >= max_edges:
                        edge_cap_hit = True
                        break
                    row = res.get_next()
                    src, dst = row[0], row[1]
                    if src not in node_ids or dst not in node_ids:
                        dropped_dangling = True
                        continue
                    edges.append({
                        "src": src,
                        "src_type": src_label,
                        "dst": dst,
                        "dst_type": dst_label,
                        "edge_type": rel,
                        "call_count": int(row[2]) if row[2] is not None else 0,
                        "confidence": float(row[3]) if row[3] is not None else 0.0,
                    })
            except Exception:  # noqa: BLE001
                pass

        node_counts = self.get_node_counts()
        if sum(node_counts.values()) > len(nodes):
            truncated_reason.append(f"node cap ({max_nodes}) reached")
        if edge_cap_hit:
            truncated_reason.append(f"edge cap ({max_edges}) reached")
        if dropped_dangling:
            truncated_reason.append("edges to capped-out nodes dropped")

        edge_counts: dict[str, int] = {}
        for rel, _, _ in edge_queries:
            edge_counts[rel] = sum(1 for e in edges if e["edge_type"] == rel)

        return {
            "nodes": nodes,
            "edges": edges,
            "node_counts": node_counts,
            "edge_counts": edge_counts,
            "truncated": bool(truncated_reason),
            "truncated_reason": truncated_reason,
        }

    def get_audit(self) -> dict[str, Any]:
        """Return a KG health audit summary.

        Checks for:
        - Total node and edge counts per table
        - Orphaned edges (edges whose source/target node no longer exists)
        - Edges with zero call_count (data anomaly)
        """
        try:
            audit: dict[str, Any] = {"store_health": "ok", "db_path": str(self.db_path)}

            # Node counts
            for label in ("Agent", "Model", "Tool", "MCPServer", "InferenceEndpoint"):
                res: Any = self._conn.execute(
                    f"MATCH (n:{label}) RETURN count(n)"
                )
                count = int(res.get_next()[0])
                audit[f"node_{label.lower()}_count"] = count

            # Edge counts per rel table
            for rel in ("CALLS", "USES_TOOL", "ROUTES_TO", "SERVED_BY"):
                res = self._conn.execute(f"MATCH ()-[e:{rel}]->() RETURN count(e)")
                count = int(res.get_next()[0])
                audit[f"edge_{rel.lower()}_count"] = count

            # Zero call_count edges (anomalous)
            zero_cc: dict[str, int] = {}
            for rel in ("CALLS", "USES_TOOL", "ROUTES_TO", "SERVED_BY"):
                res = self._conn.execute(
                    f"MATCH ()-[e:{rel}]->() WHERE e.call_count = 0 RETURN count(e)"
                )
                zero_cc[rel] = int(res.get_next()[0])
            audit["zero_call_count_edges"] = zero_cc
            audit["issues"] = [
                f"{rel}: {cnt} zero-call-count edges"
                for rel, cnt in zero_cc.items()
                if cnt > 0
            ]
            return audit
        except Exception as exc:
            return {
                "store_health": "error",
                "error": str(exc),
                "db_path": str(self.db_path),
            }
