"""KuzuDB lineage backend — embedded graph DB, production-candidate v2 tier.

Ships as *production-candidate* pending benchmark confirmation of
depth-5 p99 < 500ms at 10M edges (Phase 6 cap-003).
See the private design/adr/0053-lineagestore-v2-tiering.md for the gate condition.

Storage format: a single generic ``Node`` table keyed by ``node_id_for(kind, ref)``,
mirroring the SQLite, Postgres and AGE backends. The pre-0.102 format declared only a
``Run`` table and is migrated on open — see ADR 0266.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from novafabric.lineage._types import LineageEdge, node_from_edge_dict, node_id_for
from novafabric.lineage.store import AbstractLineageStore, NodeRow

logger = logging.getLogger(__name__)

_MISSING = "kuzu not installed; run: uv add kuzu"

#: Kuzu's binder rejects a variable-length upper bound above 30
#: ("Binder exception: Upper bound of rel e exceeds maximum: 30"), so this is
#: an engine ceiling, not a tuning choice. The SQLite reference bounds its
#: recursive CTE at 100, so a replay chain longer than 30 hops is truncated
#: here and not there — a documented backend limit (ADR 0266), not a silent one.
_MAX_REPLAY_DEPTH = 30

_DDL_NODE = (
    "CREATE NODE TABLE IF NOT EXISTS Node("
    "node_id STRING, kind STRING, ref STRING, PRIMARY KEY(node_id))"
)
_DDL_REL = (
    "CREATE REL TABLE IF NOT EXISTS LEDGE("
    "FROM Node TO Node, "
    "edge_id STRING, "
    "edge_type STRING, "
    "depth INT32, "
    "signature_ref STRING, "
    "capsule_run_id STRING, "
    "site_id STRING"
    ")"
)

#: The pre-0.102 tables. Their presence is what identifies a legacy database.
_LEGACY_NODE_TABLE = "Run"
_LEGACY_REL_TABLE = "LINEAGE"


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
        self._migrate_legacy_schema()

    # ------------------------------------------------------------------
    # AbstractLineageStore interface
    # ------------------------------------------------------------------

    def insert(self, edge: LineageEdge) -> None:
        """Persist *edge* (and its implied nodes) idempotently."""
        src = node_from_edge_dict(edge.source)
        tgt = node_from_edge_dict(edge.target)

        for node in {src.node_id: src, tgt.node_id: tgt}.values():
            self._conn.execute(
                "MERGE (n:Node {node_id: $nid}) SET n.kind = $kind, n.ref = $ref",
                {"nid": node.node_id, "kind": node.kind, "ref": node.ref},
            )

        # Check for an existing rel with the same edge_id before creating.
        res: Any = self._conn.execute(
            "MATCH (a:Node)-[e:LEDGE]->(b:Node) "
            "WHERE a.node_id = $src AND b.node_id = $tgt AND e.edge_id = $eid "
            "RETURN count(*)",
            {"src": src.node_id, "tgt": tgt.node_id, "eid": edge.edge_id},
        )
        if res.get_next()[0] == 0:
            self._conn.execute(
                "MATCH (a:Node {node_id: $src}), (b:Node {node_id: $tgt}) "
                "CREATE (a)-[:LEDGE {"
                "edge_id: $eid, "
                "edge_type: $et, "
                "depth: 1, "
                "signature_ref: $sig, "
                "capsule_run_id: $crid, "
                "site_id: $sid"
                "}]->(b)",
                {
                    "src": src.node_id,
                    "tgt": tgt.node_id,
                    "eid": edge.edge_id,
                    "et": edge.edge_type,
                    "sig": "",
                    "crid": edge.capsule_run_id,
                    "sid": self.site_id,
                },
            )

    def provenance(self, run_id: str, depth: int) -> list[NodeRow]:
        """Return nodes reachable forward from *run_id* up to *depth* hops."""
        hops = max(1, int(depth))
        res: Any = self._conn.execute(
            f"MATCH (start:Node {{node_id: $nid}})-[:LEDGE*1..{hops}]->(n:Node) "
            "RETURN DISTINCT n.node_id, n.kind, n.ref",
            {"nid": node_id_for("run", run_id)},
        )
        return self._collect_rows(res)

    def blast_radius(self, run_id: str, max_depth: int) -> list[NodeRow]:
        """Return upstream nodes that could have influenced *run_id*."""
        hops = max(1, int(max_depth))
        res: Any = self._conn.execute(
            f"MATCH (start:Node {{node_id: $nid}})<-[:LEDGE*1..{hops}]-(n:Node) "
            "RETURN DISTINCT n.node_id, n.kind, n.ref",
            {"nid": node_id_for("run", run_id)},
        )
        return self._collect_rows(res)

    def replay_chain(self, run_id: str) -> list[NodeRow]:
        """Return ``replayed_from`` ancestors of *run_id*, nearest first.

        Order is contractual — the chain is rendered to users as "D was replayed
        from C, which came from B" — so this orders by hop distance and breaks
        ties on ``ref`` to stay deterministic. Before ADR 0266 the query was a
        bare ``DISTINCT`` match with no ``ORDER BY``, which returned the
        ancestors shuffled (measured: 5 distinct orderings in 40 runs).

        The start node is excluded so a cyclic graph cannot report a run as its
        own ancestor.
        """
        start_id = node_id_for("run", run_id)
        res: Any = self._conn.execute(
            f"MATCH (start:Node {{node_id: $nid}})-[e:LEDGE*1..{_MAX_REPLAY_DEPTH} "
            "(r, _ | WHERE r.edge_type = 'replayed_from')]->(n:Node) "
            "WHERE n.node_id <> $nid "
            "RETURN n.node_id, n.kind, n.ref, min(length(e)) AS step "
            "ORDER BY step, n.ref",
            {"nid": start_id},
        )
        rows: list[NodeRow] = []
        while res.has_next():
            node_id, kind, ref, step = res.get_next()
            rows.append(
                {"node_id": node_id, "kind": kind, "ref": ref, "step": step}
            )
        return rows

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_rows(self, res: Any) -> list[NodeRow]:
        rows: list[NodeRow] = []
        while res.has_next():
            node_id, kind, ref = res.get_next()
            rows.append({"node_id": node_id, "kind": kind, "ref": ref})
        return rows

    def _table_names(self) -> set[str]:
        res: Any = self._conn.execute("CALL show_tables() RETURN *")
        names: set[str] = set()
        while res.has_next():
            row = res.get_next()
            # (id, name, type, ...) — the name is the second column.
            names.add(str(row[1]))
        return names

    def _migrate_legacy_schema(self) -> None:
        """Copy a pre-0.102 ``Run``/``LINEAGE`` database into the generic model.

        The legacy schema had no place for a non-run node: an asset endpoint was
        stored as a ``Run`` with an empty ``run_id``, so its ref was *never
        persisted* and cannot be recovered here. Those rows are reported rather
        than dropped silently — re-ingest is the only way to restore them.
        """
        names = self._table_names()
        if _LEGACY_NODE_TABLE not in names:
            return

        res: Any = self._conn.execute(
            f"MATCH (n:{_LEGACY_NODE_TABLE}) RETURN n.run_id"
        )
        legacy_runs: list[str] = []
        while res.has_next():
            legacy_runs.append(res.get_next()[0])

        legacy_edges: list[tuple[str, ...]] = []
        if _LEGACY_REL_TABLE in names:
            res = self._conn.execute(
                f"MATCH (a:{_LEGACY_NODE_TABLE})-[e:{_LEGACY_REL_TABLE}]->"
                f"(b:{_LEGACY_NODE_TABLE}) "
                "RETURN a.run_id, b.run_id, e.edge_id, e.edge_type, "
                "e.capsule_run_id, e.site_id"
            )
            while res.has_next():
                legacy_edges.append(tuple(res.get_next()))

        for run_id in legacy_runs:
            if not run_id:
                continue
            self._conn.execute(
                "MERGE (n:Node {node_id: $nid}) SET n.kind = 'run', n.ref = $ref",
                {"nid": node_id_for("run", run_id), "ref": run_id},
            )

        unrecoverable = 0
        for src, tgt, edge_id, edge_type, capsule_run_id, site_id in legacy_edges:
            if not src or not tgt:
                unrecoverable += 1
                continue
            self._conn.execute(
                "MATCH (a:Node {node_id: $src}), (b:Node {node_id: $tgt}) "
                "CREATE (a)-[:LEDGE {"
                "edge_id: $eid, edge_type: $et, depth: 1, signature_ref: '', "
                "capsule_run_id: $crid, site_id: $sid}]->(b)",
                {
                    "src": node_id_for("run", src),
                    "tgt": node_id_for("run", tgt),
                    "eid": edge_id,
                    "et": edge_type,
                    "crid": capsule_run_id,
                    "sid": site_id,
                },
            )

        if _LEGACY_REL_TABLE in names:
            self._conn.execute(f"DROP TABLE {_LEGACY_REL_TABLE}")
        self._conn.execute(f"DROP TABLE {_LEGACY_NODE_TABLE}")

        logger.warning(
            "migrated legacy kuzu lineage schema (ADR 0266): "
            "%d run nodes and %d edges copied; %d edges could not be migrated "
            "because the legacy schema never persisted their non-run endpoint "
            "(re-ingest those capsules to restore them)",
            len([r for r in legacy_runs if r]),
            len(legacy_edges) - unrecoverable,
            unrecoverable,
        )
