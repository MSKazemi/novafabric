"""SPKG operational graph store — embedded KùzuDB LPG (ADR-0111 D2, spike SP-1).

The operational view of the Security & Provenance Knowledge Graph: a labeled-property
graph over NovaFabric entities (agent/run/tool/model/dataset/artifact) and their typed
edges, backed by embedded KùzuDB (MIT, Tier-A) — the same engine as the KuzuDB lineage
tier (ADR-0053/0109), reused rather than a second store. Powers attack-path / blast-radius
traversal (UC2/UC3).

Requires the optional ``spkg`` extra (installs ``kuzu``); imported lazily.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Iterable

from novafabric.lineage._types import node_id_for

_MISSING = "kuzu not installed; run: uv sync --extra spkg"

_DDL_NODE = (
    "CREATE NODE TABLE IF NOT EXISTS Entity("
    "node_id STRING, kind STRING, ref STRING, PRIMARY KEY(node_id))"
)
_DDL_REL = (
    "CREATE REL TABLE IF NOT EXISTS EDGE("
    "FROM Entity TO Entity, edge_type STRING, capsule_run_id STRING, created_at STRING)"
)


def _node_id(node: dict[str, Any]) -> str:
    return str(
        node.get("node_id")
        or node_id_for(str(node.get("kind", "")), str(node.get("ref", "")))
    )


class SpkgGraphStore:
    """Embedded KùzuDB-backed SPKG graph store."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        try:
            import kuzu
        except ImportError as exc:  # pragma: no cover - env guard
            raise ImportError(_MISSING) from exc

        self._tmp_dir: str | None = None
        if db_path is None:
            self._tmp_dir = tempfile.mkdtemp()
            db_path = Path(self._tmp_dir) / "spkg.kuzu"
        self._db = kuzu.Database(str(db_path))
        self._conn = kuzu.Connection(self._db)
        self._conn.execute(_DDL_NODE)
        self._conn.execute(_DDL_REL)

    def ingest_edges(self, edges: Iterable[dict[str, Any]]) -> int:
        """Ingest lineage-shaped edge dicts. Returns the number of edges written."""
        n = 0
        for edge in edges:
            src = edge.get("source", {}) or {}
            tgt = edge.get("target", {}) or {}
            s_id, t_id = _node_id(src), _node_id(tgt)
            self._merge_node(s_id, str(src.get("kind", "")), str(src.get("ref", "")))
            self._merge_node(t_id, str(tgt.get("kind", "")), str(tgt.get("ref", "")))
            self._conn.execute(
                "MATCH (a:Entity {node_id: $s}), (b:Entity {node_id: $t}) "
                "CREATE (a)-[:EDGE {edge_type: $et, capsule_run_id: $c, created_at: $ca}]->(b)",
                {
                    "s": s_id,
                    "t": t_id,
                    "et": str(edge.get("edge_type", "")),
                    "c": str(edge.get("capsule_run_id", "")),
                    "ca": str(edge.get("created_at", "")),
                },
            )
            n += 1
        return n

    def _merge_node(self, node_id: str, kind: str, ref: str) -> None:
        self._conn.execute(
            "MERGE (n:Entity {node_id: $id}) SET n.kind = $k, n.ref = $r",
            {"id": node_id, "k": kind, "r": ref},
        )

    def attack_path(
        self, source_id: str, target_id: str, max_depth: int = 6
    ) -> int | None:
        """Shortest attack path hop-count from *source_id* to *target_id*, or None.

        Uses KùzuDB bounded shortest-path recursion (UC2 lateral-movement query).
        """
        res: Any = self._conn.execute(
            "MATCH p = (a:Entity {node_id: $s})"
            f"-[:EDGE* SHORTEST 1..{int(max_depth)}]->(b:Entity {{node_id: $t}}) "
            "RETURN length(p) AS hops LIMIT 1",
            {"s": source_id, "t": target_id},
        )
        if res.has_next():
            return int(res.get_next()[0])
        return None

    def descendants(
        self, node_id: str, max_depth: int = 6
    ) -> list[tuple[str, str, str]]:
        """Entities reachable *forward* from *node_id* — the blast radius (UC3).

        Returns ``(node_id, kind, ref)`` tuples (deduplicated, excluding the start node).
        """
        res: Any = self._conn.execute(
            "MATCH (a:Entity {node_id: $s})"
            f"-[:EDGE*1..{int(max_depth)}]->(n:Entity) "
            "RETURN DISTINCT n.node_id, n.kind, n.ref",
            {"s": node_id},
        )
        return self._rows(res)

    def ancestors(
        self, node_id: str, max_depth: int = 6
    ) -> list[tuple[str, str, str]]:
        """Entities that can reach *node_id* — its provenance / upstream influence."""
        res: Any = self._conn.execute(
            "MATCH (n:Entity)"
            f"-[:EDGE*1..{int(max_depth)}]->(b:Entity {{node_id: $t}}) "
            "RETURN DISTINCT n.node_id, n.kind, n.ref",
            {"t": node_id},
        )
        return self._rows(res)

    def _rows(self, res: Any) -> list[tuple[str, str, str]]:
        out: list[tuple[str, str, str]] = []
        while res.has_next():
            r = res.get_next()
            out.append((str(r[0]), str(r[1]), str(r[2])))
        return out

    def node_count(self) -> int:
        res: Any = self._conn.execute("MATCH (n:Entity) RETURN COUNT(n)")
        return int(res.get_next()[0]) if res.has_next() else 0

    def edge_count(self) -> int:
        res: Any = self._conn.execute("MATCH ()-[e:EDGE]->() RETURN COUNT(e)")
        return int(res.get_next()[0]) if res.has_next() else 0

    def close(self) -> None:
        # KùzuDB closes on GC; drop references so a temp dir can be cleaned.
        self._conn = None  # type: ignore[assignment]
        self._db = None  # type: ignore[assignment]
