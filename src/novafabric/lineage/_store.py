# src/novafabric/lineage/_store.py
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from novafabric.lineage._types import (
    LineageEdge,
    LineageNode,
    node_from_edge_dict,
)
from novafabric.registry.store import get_connection

#: Node identity is defined once, in ``_types``; every backend shares it.
_make_node_from_edge_dict = node_from_edge_dict


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


class LineageGraphTooLargeError(RuntimeError):
    """Raised when a whole-graph read would exceed its explicit bound (ADR-0212).

    Silent truncation would make every downstream metric quietly wrong, so an
    oversize graph fails loudly instead.
    """


class LineageStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self._conn = get_connection(db_path)
        self._migrate()
        self._conn.execute("PRAGMA foreign_keys = ON")

    def _migrate(self) -> None:
        self._conn.executescript(_DDL)
        self._conn.commit()

    def replace_capsule_lineage(
        self,
        nodes: list[LineageNode],
        edges: list[LineageEdge],
        capsule_run_id: str,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM lineage_edges WHERE capsule_run_id = ?",
                (capsule_run_id,),
            )
            for node in nodes:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO lineage_nodes
                        (node_id, kind, ref, first_seen_capsule_run_id, payload)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        node.node_id,
                        node.kind,
                        node.ref,
                        node.first_seen_capsule_run_id,
                        json.dumps(node.payload),
                    ),
                )
            for edge in edges:
                src_node_id = self._resolve_node_id(edge.source, nodes)
                tgt_node_id = self._resolve_node_id(edge.target, nodes)
                self._conn.execute(
                    """
                    INSERT INTO lineage_edges
                        (edge_id, edge_type, source_id, target_id,
                         capsule_run_id, confidence, created_at, payload)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        edge.edge_id,
                        edge.edge_type,
                        src_node_id,
                        tgt_node_id,
                        capsule_run_id,
                        edge.confidence,
                        edge.created_at,
                        json.dumps(edge.as_dict()),
                    ),
                )

    def _resolve_node_id(
        self,
        node_dict: dict[str, Any],
        nodes: list[LineageNode],
    ) -> str:
        return node_from_edge_dict(node_dict).node_id

    def _node_id_for_ref(self, ref: str, kind: str | None) -> str | None:
        if kind:
            row = self._conn.execute(
                "SELECT node_id FROM lineage_nodes WHERE ref = ? AND kind = ?",
                (ref, kind),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT node_id FROM lineage_nodes WHERE ref = ?",
                (ref,),
            ).fetchone()
        return dict(row)["node_id"] if row else None

    def has_node(self, ref: str, kind: str | None = None) -> bool:
        """Is *ref* present in the graph at all?

        Needed because an empty :meth:`provenance` result is ambiguous: it means *"this ref has no
        ancestors"* **and** *"this ref is not in the graph"*. A caller that diffs two empty
        ancestor lists concludes "nothing changed" — a finding manufactured from missing data.

        Delegates to the same lookup :meth:`provenance` uses, so node identity keeps one
        definition rather than gaining another copy.
        """
        return self._node_id_for_ref(ref, kind) is not None

    def _rows_to_dicts(self, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(r) for r in rows]

    def edges_for_nodes(self, node_ids: list[str]) -> list[dict[str, Any]]:
        """Edge payload dicts whose endpoints are both in *node_ids*.

        Additive read surface for ``--with-facets`` (ADR-0090); the payload
        round-trips the full edge dict, including ``facets``.
        """
        if not node_ids:
            return []
        placeholders = ",".join("?" for _ in node_ids)
        rows = self._conn.execute(
            f"""
            SELECT payload FROM lineage_edges
            WHERE source_id IN ({placeholders})
              AND target_id IN ({placeholders})
            """,
            (*node_ids, *node_ids),
        ).fetchall()
        return [json.loads(dict(r)["payload"]) for r in rows]

    def all_nodes(self, *, limit: int = 250_000) -> list[dict[str, Any]]:
        """Every lineage node as a dict, ordered by ``node_id`` (ADR-0212).

        Whole-graph read surface for the analytics layer. Unlike the rooted
        traversals, ``payload`` is returned parsed (a dict, not JSON text).
        Raises :class:`LineageGraphTooLargeError` above *limit*.
        """
        (count,) = self._conn.execute(
            "SELECT COUNT(*) FROM lineage_nodes"
        ).fetchone()
        if count > limit:
            raise LineageGraphTooLargeError(
                f"lineage graph has {count} nodes, exceeding the {limit}-node bound"
            )
        rows = self._conn.execute(
            """
            SELECT node_id, kind, ref, first_seen_capsule_run_id, payload
            FROM lineage_nodes ORDER BY node_id
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def all_edges(self, *, limit: int = 1_000_000) -> list[dict[str, Any]]:
        """Every lineage edge with endpoint ids, canonically ordered (ADR-0212).

        Ordered by ``(source_id, target_id, edge_type, edge_id)`` so downstream
        exports are byte-stable. ``payload`` is the parsed full edge dict and
        round-trips ``facets`` (ADR-0090), like :meth:`edges_for_nodes`.
        Raises :class:`LineageGraphTooLargeError` above *limit*.
        """
        (count,) = self._conn.execute(
            "SELECT COUNT(*) FROM lineage_edges"
        ).fetchone()
        if count > limit:
            raise LineageGraphTooLargeError(
                f"lineage graph has {count} edges, exceeding the {limit}-edge bound"
            )
        rows = self._conn.execute(
            """
            SELECT edge_id, edge_type, source_id, target_id, capsule_run_id,
                   confidence, created_at, payload
            FROM lineage_edges
            ORDER BY source_id, target_id, edge_type, edge_id
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def blast_radius(
        self, ref: str, kind: str | None = None, depth: int = 5,
        edge_type: str | None = None,
    ) -> list[dict[str, Any]]:
        start = self._node_id_for_ref(ref, kind)
        if start is None:
            return []
        edge_filter = "AND e.edge_type = ?" if edge_type else ""
        root_filter = "AND edge_type = ?" if edge_type else ""
        et_param: tuple[Any, ...] = (edge_type,) if edge_type else ()
        params: tuple[Any, ...] = (start,) + et_param + (depth,) + et_param
        rows = self._conn.execute(
            f"""
            WITH RECURSIVE blast AS (
                SELECT source_id, 1 AS depth, ',' || source_id || ',' AS path
                FROM lineage_edges WHERE target_id = ? {root_filter}
                UNION ALL
                SELECT e.source_id, b.depth + 1, b.path || e.source_id || ','
                FROM lineage_edges e JOIN blast b ON e.target_id = b.source_id
                WHERE b.depth < ? {edge_filter}
                  AND instr(b.path, ',' || e.source_id || ',') = 0
            )
            SELECT DISTINCT n.node_id, n.kind, n.ref, n.payload
            FROM blast JOIN lineage_nodes n ON n.node_id = blast.source_id
            """,
            params,
        ).fetchall()
        return self._rows_to_dicts(rows)

    def provenance(
        self, ref: str, kind: str | None = None, depth: int = 5,
        edge_type: str | None = None,
    ) -> list[dict[str, Any]]:
        start = self._node_id_for_ref(ref, kind)
        if start is None:
            return []
        edge_filter = "AND e.edge_type = ?" if edge_type else ""
        root_filter = "AND edge_type = ?" if edge_type else ""
        et_param_prov: tuple[Any, ...] = (edge_type,) if edge_type else ()
        params = (start,) + et_param_prov + (depth,) + et_param_prov
        rows = self._conn.execute(
            f"""
            WITH RECURSIVE prov AS (
                SELECT target_id, 1 AS depth, ',' || target_id || ',' AS path
                FROM lineage_edges WHERE source_id = ? {root_filter}
                UNION ALL
                SELECT e.target_id, p.depth + 1, p.path || e.target_id || ','
                FROM lineage_edges e JOIN prov p ON e.source_id = p.target_id
                WHERE p.depth < ? {edge_filter}
                  AND instr(p.path, ',' || e.target_id || ',') = 0
            )
            SELECT DISTINCT n.node_id, n.kind, n.ref, n.payload
            FROM prov JOIN lineage_nodes n ON n.node_id = prov.target_id
            """,
            params,
        ).fetchall()
        return self._rows_to_dicts(rows)

    def replay_chain(self, run_id: str) -> list[dict[str, Any]]:
        start = self._node_id_for_ref(run_id, "run")
        if start is None:
            return []
        rows = self._conn.execute(
            """
            WITH RECURSIVE chain AS (
                SELECT source_id, target_id, 1 AS step,
                       ',' || source_id || ',' || target_id || ',' AS path
                FROM lineage_edges
                WHERE edge_type = 'replayed_from' AND source_id = ?
                UNION ALL
                SELECT e.source_id, e.target_id, c.step + 1,
                       c.path || e.target_id || ','
                FROM lineage_edges e JOIN chain c ON e.source_id = c.target_id
                WHERE e.edge_type = 'replayed_from' AND c.step < 100
                  AND instr(c.path, ',' || e.target_id || ',') = 0
            )
            SELECT n.node_id, n.kind, n.ref, n.payload, c.step
            FROM chain c JOIN lineage_nodes n ON n.node_id = c.target_id
            ORDER BY c.step
            """,
            (start,),
        ).fetchall()
        return self._rows_to_dicts(rows)

    def insert_edge(self, edge: LineageEdge) -> None:
        """Persist *edge* and its implied nodes idempotently using ``INSERT OR IGNORE``."""
        src_node = _make_node_from_edge_dict(edge.source)
        tgt_node = _make_node_from_edge_dict(edge.target)
        with self._conn:
            for node in {src_node.node_id: src_node, tgt_node.node_id: tgt_node}.values():
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO lineage_nodes
                        (node_id, kind, ref, first_seen_capsule_run_id, payload)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        node.node_id,
                        node.kind,
                        node.ref,
                        node.first_seen_capsule_run_id,
                        json.dumps(node.payload),
                    ),
                )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO lineage_edges
                    (edge_id, edge_type, source_id, target_id,
                     capsule_run_id, confidence, created_at, payload)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge.edge_id,
                    edge.edge_type,
                    src_node.node_id,
                    tgt_node.node_id,
                    edge.capsule_run_id,
                    edge.confidence,
                    edge.created_at,
                    json.dumps(edge.as_dict()),
                ),
            )

    def build_hot_index(self, max_nodes: int = 100_000) -> Any:
        """Construct an optional hot in-memory impact index from this store.

        Additive, opt-in (ADR-0083, gap-013): nothing calls this by default. The
        returned :class:`~novafabric.lineage._index.HotLineageIndex` is a derived,
        rebuildable cache whose ``query_blast_radius`` matches :meth:`blast_radius`.
        The durable store remains the single source of truth.
        """
        from novafabric.lineage._index import build_hot_index

        return build_hot_index(self, max_nodes=max_nodes)

    def time_travel(
        self, ref: str, asof: str, kind: str | None = None, limit: int = 5000
    ) -> list[dict[str, Any]]:
        node_id = self._node_id_for_ref(ref, kind)
        if node_id is None:
            return []
        rows = self._conn.execute(
            """
            SELECT e.edge_id, e.edge_type, e.confidence, e.created_at,
                   ns.node_id AS source_node_id, ns.kind AS source_kind, ns.ref AS source_ref,
                   nt.node_id AS target_node_id, nt.kind AS target_kind, nt.ref AS target_ref
            FROM lineage_edges e
            JOIN lineage_nodes ns ON ns.node_id = e.source_id
            JOIN lineage_nodes nt ON nt.node_id = e.target_id
            WHERE (e.source_id = ? OR e.target_id = ?)
              AND e.created_at <= ?
            ORDER BY e.created_at
            LIMIT ?
            """,
            (node_id, node_id, asof, limit),
        ).fetchall()
        return self._rows_to_dicts(rows)
