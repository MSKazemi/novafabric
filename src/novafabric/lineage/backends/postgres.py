"""Postgres lineage backend — psycopg3, recursive-CTE graph traversal (ADR-0053).

A behavioural peer of :class:`~novafabric.lineage.backends.sqlite.SqliteLineageStore`:
identical schema and identical query answers, on plain PostgreSQL (no Apache AGE
extension required — traversal is expressed with `WITH RECURSIVE` and an
array-based visited set for cycle safety). This backend is *correctness-complete*
and covered by a testcontainers parity suite; the 10M-edge depth-5 p99<500ms
**promotion** benchmark (Phase 6 B-7) is a separate infra-heavy gate and does not
block this backend from existing or being used at moderate scale.

Threading: a single connection guarded by a lock. The at-scale production variant
should swap in a connection pool; that is a promotion concern, not a correctness one.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from novafabric.lineage._types import LineageEdge, LineageNode, node_id_for
from novafabric.lineage.store import AbstractLineageStore, NodeRow

_DDL = """
CREATE TABLE IF NOT EXISTS lineage_nodes (
    node_id                   TEXT PRIMARY KEY,
    kind                      TEXT NOT NULL,
    ref                       TEXT NOT NULL,
    first_seen_capsule_run_id TEXT,
    payload                   TEXT NOT NULL,
    UNIQUE(kind, ref)
);
CREATE TABLE IF NOT EXISTS lineage_edges (
    edge_id        TEXT PRIMARY KEY,
    edge_type      TEXT NOT NULL,
    source_id      TEXT NOT NULL REFERENCES lineage_nodes(node_id),
    target_id      TEXT NOT NULL REFERENCES lineage_nodes(node_id),
    capsule_run_id TEXT NOT NULL,
    confidence     TEXT,
    created_at     TEXT NOT NULL,
    payload        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS lineage_edges_source   ON lineage_edges(source_id);
CREATE INDEX IF NOT EXISTS lineage_edges_target   ON lineage_edges(target_id);
CREATE INDEX IF NOT EXISTS lineage_edges_capsule  ON lineage_edges(capsule_run_id);
CREATE INDEX IF NOT EXISTS lineage_nodes_ref      ON lineage_nodes(ref);
CREATE INDEX IF NOT EXISTS lineage_nodes_kind_ref ON lineage_nodes(kind, ref);
"""


def _node_from_edge_dict(node_dict: dict[str, Any]) -> LineageNode:
    """Resolve a node from an edge endpoint (mirrors ``_store`` node resolution)."""
    kind = node_dict.get("kind", "")
    if kind == "run":
        ref = node_dict.get("run_id", "")
    elif kind == "asset":
        ref = f"{node_dict.get('registry', 'local')}:{node_dict.get('asset_ref', '')}"
    elif kind == "artifact":
        art = node_dict.get("artifact_ref", {})
        ref = f"artifact:{art.get('capsule_run_id', '')}:{art.get('path', '')}"
    else:
        ref = node_dict.get("ref", str(node_dict))
    return LineageNode(
        node_id=node_id_for(kind, ref),
        kind=kind,
        ref=ref,
        first_seen_capsule_run_id=node_dict.get("capsule_run_id"),
        payload=node_dict,
    )


class PostgresLineageStore(AbstractLineageStore):
    """psycopg3-backed lineage store with recursive-CTE traversal."""

    def __init__(self, dsn: str, site_id: str = "local") -> None:
        import psycopg  # local import: psycopg is an optional (server-extra) dependency

        self._dsn = dsn
        self.site_id = site_id
        self._lock = threading.Lock()
        self._conn = psycopg.connect(dsn, autocommit=True)
        with self._lock:
            self._conn.execute(_DDL)

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # AbstractLineageStore interface
    # ------------------------------------------------------------------

    def insert(self, edge: LineageEdge) -> None:
        """Persist *edge* and its implied nodes idempotently (``ON CONFLICT DO NOTHING``)."""
        src = _node_from_edge_dict(edge.source)
        tgt = _node_from_edge_dict(edge.target)
        with self._lock, self._conn.transaction():
            for node in {src.node_id: src, tgt.node_id: tgt}.values():
                self._conn.execute(
                    """
                    INSERT INTO lineage_nodes
                        (node_id, kind, ref, first_seen_capsule_run_id, payload)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (node_id) DO NOTHING
                    """,
                    (node.node_id, node.kind, node.ref,
                     node.first_seen_capsule_run_id, json.dumps(node.payload)),
                )
            self._conn.execute(
                """
                INSERT INTO lineage_edges
                    (edge_id, edge_type, source_id, target_id,
                     capsule_run_id, confidence, created_at, payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (edge_id) DO NOTHING
                """,
                (edge.edge_id, edge.edge_type, src.node_id, tgt.node_id,
                 edge.capsule_run_id, edge.confidence, edge.created_at,
                 json.dumps(edge.as_dict())),
            )

    def provenance(self, run_id: str, depth: int) -> list[NodeRow]:
        """Nodes reachable forward from *run_id* (source→target) up to *depth* hops."""
        return self._traverse(run_id, depth, forward=True)

    def blast_radius(self, run_id: str, max_depth: int) -> list[NodeRow]:
        """Upstream nodes that could have influenced *run_id* (target→source)."""
        return self._traverse(run_id, max_depth, forward=False)

    def replay_chain(self, run_id: str) -> list[NodeRow]:
        """Ordered ``replayed_from`` chain anchored at *run_id* (matches SQLite)."""
        start = self._node_id_for_ref(run_id, "run")
        if start is None:
            return []
        sql = """
            WITH RECURSIVE chain AS (
                SELECT source_id, target_id, 1 AS step, ARRAY[source_id, target_id] AS path
                FROM lineage_edges
                WHERE edge_type = 'replayed_from' AND source_id = %s
                UNION ALL
                SELECT e.source_id, e.target_id, c.step + 1, c.path || e.target_id
                FROM lineage_edges e JOIN chain c ON e.source_id = c.target_id
                WHERE e.edge_type = 'replayed_from' AND c.step < 100
                  AND NOT (e.target_id = ANY(c.path))
            )
            SELECT n.node_id, n.kind, n.ref, n.payload, c.step
            FROM chain c JOIN lineage_nodes n ON n.node_id = c.target_id
            ORDER BY c.step
        """
        with self._lock:
            rows = self._conn.execute(sql, (start,)).fetchall()
        return [
            {"node_id": r[0], "kind": r[1], "ref": r[2], "payload": r[3], "step": r[4]}
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _node_id_for_ref(self, ref: str, kind: str | None) -> str | None:
        with self._lock:
            if kind:
                row = self._conn.execute(
                    "SELECT node_id FROM lineage_nodes WHERE ref = %s AND kind = %s",
                    (ref, kind),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT node_id FROM lineage_nodes WHERE ref = %s",
                    (ref,),
                ).fetchone()
        return row[0] if row else None

    def _traverse(self, run_id: str, depth: int, *, forward: bool) -> list[NodeRow]:
        """Recursive-CTE reachability with an array-based visited set (cycle-safe).

        Forward (provenance): seed on ``source_id = start``, collect ``target_id``,
        recurse by joining ``e.source_id = w.node``. Backward (blast radius): seed
        on ``target_id = start``, collect ``source_id``, recurse by joining
        ``e.target_id = w.node``. In both directions the recurse-join column equals
        the seed column, and the collected column is the opposite endpoint.
        """
        start = self._node_id_for_ref(run_id, "run")
        if start is None:
            return []
        seed_col = "source_id" if forward else "target_id"
        step_col = "target_id" if forward else "source_id"
        sql = f"""
            WITH RECURSIVE walk AS (
                SELECT {step_col} AS node, 1 AS depth, ARRAY[{step_col}] AS path
                FROM lineage_edges WHERE {seed_col} = %s
                UNION ALL
                SELECT e.{step_col}, w.depth + 1, w.path || e.{step_col}
                FROM lineage_edges e JOIN walk w ON e.{seed_col} = w.node
                WHERE w.depth < %s AND NOT (e.{step_col} = ANY(w.path))
            )
            SELECT DISTINCT n.node_id, n.kind, n.ref, n.payload
            FROM walk JOIN lineage_nodes n ON n.node_id = walk.node
            ORDER BY n.node_id
        """
        with self._lock:
            rows = self._conn.execute(sql, (start, depth)).fetchall()
        return [{"node_id": r[0], "kind": r[1], "ref": r[2], "payload": r[3]} for r in rows]
