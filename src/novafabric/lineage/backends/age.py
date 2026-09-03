"""Apache AGE lineage backend — openCypher over PostgreSQL (ADR-0053).

A third behavioural peer of :class:`~novafabric.lineage.backends.sqlite.SqliteLineageStore`
(alongside the recursive-CTE :class:`~novafabric.lineage.backends.postgres.PostgresLineageStore`
and the embedded :class:`~novafabric.lineage.backends.kuzu.KuzuLineageStore`). It stores the
lineage graph as an AGE property graph (``LNode`` vertices, ``LEDGE`` relationships) and answers
``provenance`` / ``blast_radius`` / ``replay_chain`` with openCypher variable-length paths, giving
identical answers to the SQLite reference (proven by a testcontainers parity suite against the
``apache/age`` image).

This is an *optional alternative engine*: the at-scale tier is already satisfied by KuzuDB
(ADR-0053, benchmark cleared 2026-05-16) and the plain-Postgres backend. AGE is here for
deployments that standardise on the AGE extension. Threading: one locked connection; a production
variant should pool.
"""

from __future__ import annotations

import json
import threading
from typing import Any

from novafabric.lineage._types import (
    LineageEdge,
    node_from_edge_dict,
)
from novafabric.lineage.store import AbstractLineageStore, NodeRow

_GRAPH = "lineage"


#: Node identity is defined once, in ``_types``; every backend shares it.
_node_from_edge_dict = node_from_edge_dict


def _vertex_props(agtype_value: Any) -> dict[str, Any]:
    """Parse an AGE ``{...}::vertex`` agtype value into its ``properties`` dict."""
    text = agtype_value if isinstance(agtype_value, str) else str(agtype_value)
    if "::" in text:
        text = text.rsplit("::", 1)[0]
    obj = json.loads(text)
    props: dict[str, Any] = obj.get("properties", {})
    return props


def _to_node_row(agtype_value: Any) -> NodeRow:
    p = _vertex_props(agtype_value)
    return {
        "node_id": p.get("node_id"),
        "kind": p.get("kind"),
        "ref": p.get("ref"),
        "payload": p.get("payload"),
    }


class AGELineageStore(AbstractLineageStore):
    """Apache AGE (Postgres graph extension) lineage backend."""

    def __init__(self, dsn: str, site_id: str = "local") -> None:
        import psycopg  # local import: psycopg + a live AGE-enabled Postgres are required

        self._dsn = dsn
        self.site_id = site_id
        self._lock = threading.Lock()
        self._conn = psycopg.connect(dsn, autocommit=True)
        with self._lock:
            self._conn.execute("CREATE EXTENSION IF NOT EXISTS age")
            self._prepare_session(self._conn)
            exists = self._conn.execute(
                "SELECT count(*) FROM ag_catalog.ag_graph WHERE name = %s", (_GRAPH,)
            ).fetchone()
            if exists and exists[0] == 0:
                self._conn.execute("SELECT create_graph(%s)", (_GRAPH,))

    @staticmethod
    def _prepare_session(conn: Any) -> None:
        conn.execute("LOAD 'age'")
        conn.execute('SET search_path = ag_catalog, "$user", public')

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # Cypher helpers
    # ------------------------------------------------------------------

    def _cypher(self, query: str, params: dict[str, Any], *, columns: int = 1) -> list[Any]:
        # AGE requires the graph name as a *literal* (not a bound parameter), so it
        # is inlined from the module constant ``_GRAPH`` (never user input). Cypher
        # parameters are passed as a single agtype argument and referenced as $name.
        cols = ", ".join(f"c{i} agtype" for i in range(columns))
        if params:
            sql = f"SELECT * FROM cypher('{_GRAPH}', $$ {query} $$, %s::agtype) AS ({cols})"
            args: tuple[Any, ...] = (json.dumps(params),)
        else:
            sql = f"SELECT * FROM cypher('{_GRAPH}', $$ {query} $$) AS ({cols})"
            args = ()
        with self._lock:
            self._prepare_session(self._conn)
            rows = self._conn.execute(sql, args).fetchall()
        return rows

    # ------------------------------------------------------------------
    # AbstractLineageStore interface
    # ------------------------------------------------------------------

    def insert(self, edge: LineageEdge) -> None:
        """Persist *edge* and its implied nodes idempotently (``MERGE``)."""
        src = _node_from_edge_dict(edge.source)
        tgt = _node_from_edge_dict(edge.target)
        for node in {src.node_id: src, tgt.node_id: tgt}.values():
            self._cypher(
                "MERGE (n:LNode {node_id: $node_id}) "
                "SET n.kind = $kind, n.ref = $ref, n.payload = $payload",
                {
                    "node_id": node.node_id,
                    "kind": node.kind,
                    "ref": node.ref,
                    "payload": json.dumps(node.payload),
                },
            )
        self._cypher(
            "MATCH (a:LNode {node_id: $src}), (b:LNode {node_id: $tgt}) "
            "MERGE (a)-[e:LEDGE {edge_id: $edge_id}]->(b) "
            "SET e.edge_type = $edge_type",
            {
                "src": src.node_id,
                "tgt": tgt.node_id,
                "edge_id": edge.edge_id,
                "edge_type": edge.edge_type,
            },
        )

    def provenance(self, run_id: str, depth: int) -> list[NodeRow]:
        """Nodes reachable forward from *run_id* (source→target) up to *depth* hops."""
        hops = max(1, int(depth))
        rows = self._cypher(
            f"MATCH (s:LNode {{ref: $ref, kind: 'run'}})-[:LEDGE*1..{hops}]->(n:LNode) "
            "RETURN DISTINCT n",
            {"ref": run_id},
        )
        return [_to_node_row(r[0]) for r in rows]

    def blast_radius(self, run_id: str, max_depth: int) -> list[NodeRow]:
        """Upstream nodes that could have influenced *run_id* (target→source)."""
        hops = max(1, int(max_depth))
        rows = self._cypher(
            f"MATCH (s:LNode {{ref: $ref, kind: 'run'}})<-[:LEDGE*1..{hops}]-(n:LNode) "
            "RETURN DISTINCT n",
            {"ref": run_id},
        )
        return [_to_node_row(r[0]) for r in rows]

    def replay_chain(self, run_id: str) -> list[NodeRow]:
        """Ordered ``replayed_from`` chain anchored at *run_id* (matches SQLite).

        Walked one hop at a time: AGE's openCypher subset does not support the
        ``all(r IN relationships(p) ...)`` variable-length predicate, so the chain
        is followed with a single-relationship, property-filtered ``MATCH`` per
        step (bounded at 100 and cycle-guarded), which AGE supports fully and which
        reproduces the SQLite step ordering exactly.
        """
        chain: list[NodeRow] = []
        current = run_id
        seen = {current}
        for step in range(1, 101):
            rows = self._cypher(
                "MATCH (s:LNode {ref: $ref, kind: 'run'})"
                "-[:LEDGE {edge_type: 'replayed_from'}]->(n:LNode) RETURN n",
                {"ref": current},
            )
            if not rows:
                break
            node = _to_node_row(rows[0][0])
            if node["ref"] in seen:
                break
            chain.append({**node, "step": step})
            seen.add(node["ref"])
            current = node["ref"]
        return chain
