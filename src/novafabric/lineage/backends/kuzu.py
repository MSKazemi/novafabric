"""KuzuDB lineage backend — embedded graph DB, production-candidate v2 tier.

Ships as *production-candidate* pending benchmark confirmation of
depth-5 p99 < 500ms at 10M edges (Phase 6 cap-003).
See the private design/adr/0053-lineagestore-v2-tiering.md for the gate condition.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from novafabric.lineage._types import LineageEdge
from novafabric.lineage.store import AbstractLineageStore, NodeRow

_MISSING = "kuzu not installed; run: uv add kuzu"

_DDL_NODE = "CREATE NODE TABLE IF NOT EXISTS Run(run_id STRING, PRIMARY KEY(run_id))"
_DDL_REL = (
    "CREATE REL TABLE IF NOT EXISTS LINEAGE("
    "FROM Run TO Run, "
    "edge_id STRING, "
    "edge_type STRING, "
    "depth INT32, "
    "signature_ref STRING, "
    "capsule_run_id STRING, "
    "site_id STRING"
    ")"
)


class KuzuLineageStore(AbstractLineageStore):
    """KuzuDB-backed lineage store (embedded openCypher graph DB)."""

    def __init__(
        self,
        db_path: Path | None = None,
        site_id: str = "local",
    ) -> None:
        try:
            import kuzu
        except ImportError as exc:
            raise ImportError(_MISSING) from exc

        self.site_id = site_id
        self._tmp_dir: str | None = None

        if db_path is None:
            self._tmp_dir = tempfile.mkdtemp()
            db_path = Path(self._tmp_dir) / "lineage.kuzu"
        else:
            db_path = Path(db_path) / "lineage.kuzu" if db_path.is_dir() else db_path

        self._db = kuzu.Database(str(db_path))
        self._conn = kuzu.Connection(self._db)
        self._conn.execute(_DDL_NODE)
        self._conn.execute(_DDL_REL)

    # ------------------------------------------------------------------
    # AbstractLineageStore interface
    # ------------------------------------------------------------------

    def insert(self, edge: LineageEdge) -> None:
        """Persist *edge* (and implied Run nodes) idempotently."""
        src_id = edge.source.get("run_id", "")
        tgt_id = edge.target.get("run_id", "")

        self._conn.execute("MERGE (r:Run {run_id: $rid})", {"rid": src_id})
        self._conn.execute("MERGE (r:Run {run_id: $rid})", {"rid": tgt_id})

        # Check for existing rel with same edge_id before creating.
        res: Any = self._conn.execute(
            "MATCH (a:Run)-[e:LINEAGE]->(b:Run) "
            "WHERE a.run_id = $src AND b.run_id = $tgt AND e.edge_id = $eid "
            "RETURN count(*)",
            {"src": src_id, "tgt": tgt_id, "eid": edge.edge_id},
        )
        if res.get_next()[0] == 0:
            self._conn.execute(
                "MATCH (a:Run {run_id: $src}), (b:Run {run_id: $tgt}) "
                "CREATE (a)-[:LINEAGE {"
                "edge_id: $eid, "
                "edge_type: $et, "
                "depth: 1, "
                "signature_ref: $sig, "
                "capsule_run_id: $crid, "
                "site_id: $sid"
                "}]->(b)",
                {
                    "src": src_id,
                    "tgt": tgt_id,
                    "eid": edge.edge_id,
                    "et": edge.edge_type,
                    "sig": "",
                    "crid": edge.capsule_run_id,
                    "sid": self.site_id,
                },
            )

    def provenance(self, run_id: str, depth: int) -> list[NodeRow]:
        """Return nodes reachable forward from *run_id* up to *depth* hops."""
        res: Any = self._conn.execute(
            f"MATCH (start:Run {{run_id: $rid}})-[:LINEAGE*1..{depth}]->(n:Run) "
            "RETURN DISTINCT n.run_id AS node_id",
            {"rid": run_id},
        )
        return self._collect_rows(res)

    def blast_radius(self, run_id: str, max_depth: int) -> list[NodeRow]:
        """Return upstream nodes that could have influenced *run_id*."""
        res: Any = self._conn.execute(
            f"MATCH (n:Run)-[:LINEAGE*1..{max_depth}]->(target:Run {{run_id: $rid}}) "
            "RETURN DISTINCT n.run_id AS node_id",
            {"rid": run_id},
        )
        return self._collect_rows(res)

    def replay_chain(self, run_id: str) -> list[NodeRow]:
        """Return nodes linked via replayed_from edges anchored at *run_id*."""
        res: Any = self._conn.execute(
            "MATCH (start:Run {run_id: $rid})"
            "-[e:LINEAGE*1..30 {edge_type: 'replayed_from'}]->(n:Run) "
            "RETURN DISTINCT n.run_id AS node_id",
            {"rid": run_id},
        )
        return self._collect_rows(res)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_rows(self, res: Any) -> list[NodeRow]:
        rows: list[NodeRow] = []
        while res.has_next():
            node_id: str = res.get_next()[0]
            rows.append({"node_id": node_id, "kind": "run", "ref": node_id})
        return rows
