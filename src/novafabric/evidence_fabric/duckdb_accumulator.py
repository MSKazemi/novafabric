"""DuckDB Arrow accumulator for Evidence Fabric self-contained mode.

Provides in-process DuckDB storage for lineage edges and capsule events,
with PyArrow batch writes and aggregation queries.  No external ClickHouse
or NATS required for local operation.

cap-002 / ADR-0066 (Evidence Fabric v1.0)
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL — two tables created on init
# ---------------------------------------------------------------------------

_DDL_LINEAGE_EDGES = """\
CREATE TABLE IF NOT EXISTS lineage_edges (
    from_ref   TEXT      NOT NULL,
    to_ref     TEXT      NOT NULL,
    edge_type  TEXT      NOT NULL,
    tenant     TEXT      NOT NULL,
    created_at TIMESTAMP DEFAULT now(),
    meta       JSON
);
"""

_DDL_CAPSULE_EVENTS = """\
CREATE TABLE IF NOT EXISTS capsule_events (
    run_id       TEXT      NOT NULL,
    event_type   TEXT      NOT NULL,
    model        TEXT,
    tool_name    TEXT,
    tenant       TEXT      NOT NULL,
    ts           TIMESTAMP DEFAULT now(),
    tokens_in    INTEGER   DEFAULT 0,
    tokens_out   INTEGER   DEFAULT 0
);
"""

# PyArrow schemas aligned to the DDL above (minus server-side DEFAULT columns
# which we supply explicitly in writes).
_ARROW_SCHEMA_LINEAGE = pa.schema(
    [
        pa.field("from_ref", pa.string()),
        pa.field("to_ref", pa.string()),
        pa.field("edge_type", pa.string()),
        pa.field("tenant", pa.string()),
        pa.field("created_at", pa.timestamp("us")),
        pa.field("meta", pa.string(), nullable=True),  # JSON serialised string; nullable
    ]
)

_ARROW_SCHEMA_CAPSULE = pa.schema(
    [
        pa.field("run_id", pa.string()),
        pa.field("event_type", pa.string()),
        pa.field("model", pa.string()),
        pa.field("tool_name", pa.string()),
        pa.field("tenant", pa.string()),
        pa.field("ts", pa.timestamp("us")),
        pa.field("tokens_in", pa.int32()),
        pa.field("tokens_out", pa.int32()),
    ]
)


class DuckDBAccumulator:
    """In-process DuckDB store for lineage edges and capsule events.

    Thread-safe via an internal ``threading.Lock``.  All public methods
    acquire the lock before touching the connection.

    Args:
        db_path: Path to the DuckDB file.  Use ``Path(":memory:")`` for tests.
    """

    def __init__(self, db_path: Path) -> None:
        self._path = db_path
        self._lock = threading.Lock()
        self._conn: duckdb.DuckDBPyConnection = duckdb.connect(str(db_path))
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Schema bootstrap
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.execute(_DDL_LINEAGE_EDGES)
            self._conn.execute(_DDL_CAPSULE_EVENTS)
            self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        """Create performance indexes for hot query paths.

        Safe to call multiple times — DuckDB ``IF NOT EXISTS`` is idempotent.
        Covers lineage fan-out and fan-in lookups by ``from_ref``/``to_ref``,
        and capsule event lookups by ``run_id``.
        """
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lineage_from ON lineage_edges(from_ref)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lineage_to ON lineage_edges(to_ref)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lineage_tenant ON lineage_edges(tenant)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_capsule_events_run ON capsule_events(run_id)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_capsule_events_tenant ON capsule_events(tenant)"
        )

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def ingest_edges(self, edges: list[dict[str, Any]]) -> int:
        """Bulk-ingest lineage edges.

        Each dict must contain ``from_ref``, ``to_ref``, ``edge_type``,
        ``tenant``.  Optional: ``created_at`` (datetime), ``meta`` (str/dict).

        Returns:
            Number of rows written.
        """
        if not edges:
            return 0

        import json as _json

        now = datetime.utcnow()
        rows: dict[str, list[Any]] = {
            "from_ref": [],
            "to_ref": [],
            "edge_type": [],
            "tenant": [],
            "created_at": [],
            "meta": [],
        }
        for e in edges:
            rows["from_ref"].append(e["from_ref"])
            rows["to_ref"].append(e["to_ref"])
            rows["edge_type"].append(e["edge_type"])
            rows["tenant"].append(e.get("tenant", "default"))
            ts = e.get("created_at", now)
            rows["created_at"].append(ts if isinstance(ts, datetime) else now)
            meta = e.get("meta")
            if isinstance(meta, dict):
                rows["meta"].append(_json.dumps(meta))
            elif meta:
                rows["meta"].append(str(meta))
            else:
                rows["meta"].append(None)  # NULL — DuckDB JSON rejects empty string

        arrow_table = pa.table(rows, schema=_ARROW_SCHEMA_LINEAGE)
        with self._lock:
            self._conn.register("_edges_batch", arrow_table)
            self._conn.execute(
                "INSERT INTO lineage_edges SELECT * FROM _edges_batch"
            )
            self._conn.unregister("_edges_batch")
        logger.debug("duckdb_accumulator: inserted %d lineage edges", len(edges))
        return len(edges)

    def ingest_capsule_events(self, events: list[dict[str, Any]]) -> int:
        """Bulk-ingest capsule events (tool calls, model calls).

        Each dict must contain ``run_id``, ``event_type``, ``tenant``.
        Optional: ``model`` (str), ``tool_name`` (str), ``ts`` (datetime),
        ``tokens_in`` (int), ``tokens_out`` (int).

        Returns:
            Number of rows written.
        """
        if not events:
            return 0

        now = datetime.utcnow()
        rows: dict[str, list[Any]] = {
            "run_id": [],
            "event_type": [],
            "model": [],
            "tool_name": [],
            "tenant": [],
            "ts": [],
            "tokens_in": [],
            "tokens_out": [],
        }
        for ev in events:
            rows["run_id"].append(ev["run_id"])
            rows["event_type"].append(ev["event_type"])
            rows["model"].append(ev.get("model") or "")
            rows["tool_name"].append(ev.get("tool_name") or "")
            rows["tenant"].append(ev.get("tenant", "default"))
            ts = ev.get("ts", now)
            rows["ts"].append(ts if isinstance(ts, datetime) else now)
            rows["tokens_in"].append(int(ev.get("tokens_in", 0)))
            rows["tokens_out"].append(int(ev.get("tokens_out", 0)))

        arrow_table = pa.table(rows, schema=_ARROW_SCHEMA_CAPSULE)
        with self._lock:
            self._conn.register("_events_batch", arrow_table)
            self._conn.execute(
                "INSERT INTO capsule_events SELECT * FROM _events_batch"
            )
            self._conn.unregister("_events_batch")
        logger.debug("duckdb_accumulator: inserted %d capsule events", len(events))
        return len(events)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def query_cost_report(
        self,
        tenant: str,
        start: datetime,
        end: datetime,
    ) -> dict[str, Any]:
        """Return per-model cost aggregation for a tenant within a time window.

        Queries ``capsule_events`` for rows with ``event_type='model_call'``
        and sums token counts.  Estimated cost is computed from the
        ``CostInterceptor.PRICE_TABLE`` if available, otherwise 0.0.

        Returns:
            ``{model: {"calls": N, "estimated_tokens": N, "cost_usd": float}}``
        """
        sql = """
            SELECT
                model,
                COUNT(*)               AS calls,
                SUM(tokens_in)         AS total_tokens_in,
                SUM(tokens_out)        AS total_tokens_out
            FROM capsule_events
            WHERE tenant = ?
              AND event_type = 'model_call'
              AND ts >= ?
              AND ts <= ?
              AND model IS NOT NULL
              AND model != ''
            GROUP BY model
            ORDER BY calls DESC
        """
        with self._lock:
            result = self._conn.execute(sql, [tenant, start, end]).fetchall()

        # Attempt to compute cost estimates via CostInterceptor if available.
        try:
            from novafabric.cost.interceptor import CostInterceptor  # noqa: PLC0415

            price_table = CostInterceptor.PRICE_TABLE
        except ImportError:
            price_table = {}

        report: dict[str, Any] = {}
        for model, calls, tok_in, tok_out in result:
            estimated_tokens = int(tok_in or 0) + int(tok_out or 0)
            prices = price_table.get(model)
            if prices:
                inp, outp = prices
                cost = (int(tok_in or 0) / 1000.0) * inp + (
                    int(tok_out or 0) / 1000.0
                ) * outp
            else:
                cost = 0.0
            report[model] = {
                "calls": int(calls),
                "estimated_tokens": estimated_tokens,
                "tokens_in": int(tok_in or 0),
                "tokens_out": int(tok_out or 0),
                "cost_usd": round(cost, 6),
            }
        return report

    def query_lineage_summary(
        self, tenant: str, depth: int = 3
    ) -> dict[str, int]:
        """Return blast-radius counts for the top 20 capsule refs.

        Counts how many distinct nodes are reachable *downstream* of each
        ``from_ref`` within ``depth`` hops of the lineage graph for a given
        tenant. A node's blast radius is the transitive closure of its
        ``from_ref → to_ref`` edges, capped at ``depth`` levels — i.e. direct
        children (depth 1), their children (depth 2), and so on. Cycles are
        safe: the depth cap bounds the traversal, and the count is over
        distinct reachable nodes.

        Args:
            tenant: Tenant to filter by.
            depth:  Maximum number of hops to expand (default 3). ``depth=1``
                    reproduces the direct-neighbour count. Values ``< 1`` are
                    clamped to 1.

        Returns:
            ``{capsule_ref: blast_radius_count}`` for the top 20 capsules
            ordered by blast radius descending.
        """
        max_hops = max(1, int(depth))
        # Recursive CTE walks the edge set breadth-first, tagging each reachable
        # node with the root it descends from. UNION (not UNION ALL) dedups rows
        # so a diamond/cycle can't blow up the frontier; the `hop < ?` guard
        # bounds depth. The outer query counts DISTINCT reachable nodes per root.
        sql = """
            WITH RECURSIVE reachable(root, node, hop) AS (
                SELECT from_ref AS root, to_ref AS node, 1 AS hop
                FROM lineage_edges
                WHERE tenant = ?
                UNION
                SELECT r.root, e.to_ref, r.hop + 1
                FROM reachable r
                JOIN lineage_edges e
                  ON e.from_ref = r.node AND e.tenant = ?
                WHERE r.hop < ?
            )
            SELECT root AS from_ref, COUNT(DISTINCT node) AS blast_radius
            FROM reachable
            GROUP BY root
            ORDER BY blast_radius DESC, from_ref ASC
            LIMIT 20
        """
        with self._lock:
            result = self._conn.execute(sql, [tenant, tenant, max_hops]).fetchall()
        return {row[0]: int(row[1]) for row in result}

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_arrow(self, table: str) -> pa.Table:
        """Export a full table as a PyArrow Table.

        Args:
            table: One of ``"lineage_edges"`` or ``"capsule_events"``.

        Returns:
            A ``pa.Table`` with all rows.

        Raises:
            ValueError: if *table* is not a known table name.
        """
        allowed = {"lineage_edges", "capsule_events"}
        if table not in allowed:
            raise ValueError(
                f"Unknown table {table!r}; must be one of {sorted(allowed)}"
            )
        with self._lock:
            # .arrow() returns a RecordBatchReader; call .read_all() for a pa.Table.
            return self._conn.execute(f"SELECT * FROM {table}").arrow().read_all()  # noqa: S608

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the DuckDB connection."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "DuckDBAccumulator":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
