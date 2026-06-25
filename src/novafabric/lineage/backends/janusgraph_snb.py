"""LDBC Social Network Benchmark BI queries adapted for lineage graphs.

These queries are inspired by the LDBC SNB BI query set (v1.0 specification,
https://ldbcouncil.org/benchmarks/snb/) and adapted to the NovaFabric lineage
graph schema:

    Vertex label: ``run``
    Edge labels:  contains | spawned | delegated_to | replayed_from

Each query documents the LDBC BI analogy in its docstring so the mapping is
auditable.

All methods require a *connected* :class:`JanusGraphLineageStore` (i.e. at
least one operation has been performed so ``_g`` is populated, or
``_connect()`` has been called explicitly).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from novafabric.lineage.backends.janusgraph import JanusGraphLineageStore


class LineageSnbQueries:
    """LDBC SNB BI query adaptations for the NovaFabric lineage graph.

    Usage::

        from novafabric.lineage.backends.janusgraph import JanusGraphLineageStore
        from novafabric.lineage.backends.janusgraph_snb import LineageSnbQueries

        store = JanusGraphLineageStore()
        q = LineageSnbQueries(store)
        q.bi_q1_run_frequency("2026-01-01T00:00:00Z")
    """

    def __init__(self, store: "JanusGraphLineageStore") -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _g(self) -> Any:
        """Return the Gremlin traversal source, connecting lazily."""
        self._store._connect()
        return self._store._g

    # ------------------------------------------------------------------
    # BI query adaptations
    # ------------------------------------------------------------------

    def bi_q1_run_frequency(self, since: str) -> list[dict[str, Any]]:
        """Count runs created at or after *since* (ISO-8601 UTC timestamp).

        **LDBC SNB BI Q1 analogy — Message Frequency:**
        Q1 counts messages created in each year/month bucket.  Here we count
        run vertices grouped by the hour-truncated ``created_at`` property,
        giving a time-series frequency histogram for audit and anomaly detection.

        Args:
            since: ISO-8601 timestamp string (e.g. ``"2026-01-01T00:00:00Z"``).

        Returns:
            List of ``{"bucket": str, "count": int}`` dicts sorted by bucket.
        """
        from gremlin_python.process.graph_traversal import (
            __ as AnonymousT,
        )

        raw: list[Any] = (
            self._g.V()
            .hasLabel("run")
            .has("created_at", AnonymousT.gte(since))
            .groupCount()
            .by(
                AnonymousT.values("created_at")
                # Truncate to hour prefix for bucketing (first 13 chars: "YYYY-MM-DDTHH")
                .substring(0, 13)
            )
            .order(AnonymousT.local())
            .by(AnonymousT.keys())
            .toList()
        )
        # raw is a list with one Map element from Gremlin groupCount
        if not raw:
            return []
        bucket_map: dict[str, int] = raw[0] if isinstance(raw[0], dict) else {}
        return [{"bucket": k, "count": v} for k, v in sorted(bucket_map.items())]

    def bi_q2_top_influential(self, top_n: int = 10) -> list[dict[str, Any]]:
        """Return the top *top_n* runs by number of downstream dependents.

        **LDBC SNB BI Q2 analogy — Tag Activity:**
        Q2 finds the top-K tags by message count in a date window.  Here we
        rank run vertices by their out-degree (number of directly downstream
        runs), surfacing the most "influential" nodes in the lineage DAG —
        useful for blast-radius triage.

        Args:
            top_n: How many results to return (default 10).

        Returns:
            List of ``{"run_id": str, "downstream_count": int}`` dicts,
            descending by count.
        """
        from gremlin_python.process.graph_traversal import (
            __ as AnonymousT,
        )

        results: list[Any] = (
            self._g.V()
            .hasLabel("run")
            .project("run_id", "downstream_count")
            .by("run_id")
            .by(AnonymousT.out().count())
            .order()
            .by("downstream_count", AnonymousT.desc)
            .limit(top_n)
            .toList()
        )
        return [dict(r) for r in results]

    def bi_q3_shortest_path(self, from_run: str, to_run: str) -> list[str]:
        """Return the run IDs along the shortest dependency path from *from_run* to *to_run*.

        **LDBC SNB BI Q3 analogy — Tag Evolution / Path:**
        Q3 computes paths between message clusters.  Here we find the minimal
        hop-count directed path between two runs in the lineage DAG, which is
        the critical path for root-cause tracing.

        Args:
            from_run: Source run ID.
            to_run:   Destination run ID.

        Returns:
            Ordered list of run IDs from *from_run* to *to_run* (inclusive),
            or an empty list if no path exists.
        """
        from gremlin_python.process.graph_traversal import (
            __ as AnonymousT,
        )

        raw: list[Any] = (
            self._g.V()
            .has("run_id", from_run)
            .repeat(AnonymousT.out().simplePath())
            .until(AnonymousT.has("run_id", to_run))
            .path()
            .by("run_id")
            .limit(1)
            .toList()
        )
        if not raw:
            return []
        # Gremlin Path objects expose .objects() in gremlin-python
        path_obj = raw[0]
        if hasattr(path_obj, "objects"):
            return list(path_obj.objects)
        return list(path_obj)

    def bi_q4_dependency_chains(self, run_id: str) -> list[list[str]]:
        """Return all unique delegation chains originating from *run_id*.

        **LDBC SNB BI Q4 analogy — Top Person:**
        Q4 finds activity threads started by top persons.  Here we enumerate
        every *delegated_to* chain rooted at *run_id*, which maps to
        orchestration fan-out (e.g. a coordinator distributing work to workers).

        Args:
            run_id: Root run ID.

        Returns:
            List of chains; each chain is an ordered list of run IDs
            (root to leaf).
        """
        from gremlin_python.process.graph_traversal import (
            __ as AnonymousT,
        )

        raw: list[Any] = (
            self._g.V()
            .has("run_id", run_id)
            .repeat(AnonymousT.out("delegated_to").simplePath())
            .emit()
            .path()
            .by("run_id")
            .toList()
        )
        chains: list[list[str]] = []
        for path_obj in raw:
            if hasattr(path_obj, "objects"):
                chains.append(list(path_obj.objects))
            else:
                chains.append(list(path_obj))
        return chains

    def bi_q5_tenant_activity(self, site_id: str) -> dict[str, Any]:
        """Return per-tenant run counts and edge-type distribution for *site_id*.

        **LDBC SNB BI Q5 analogy — Membership Activity:**
        Q5 measures how active forum members are.  Here we profile a single
        tenant (site) by counting its run vertices and breaking down outgoing
        edges by label — providing an operational health snapshot per site.

        Args:
            site_id: Tenant / site identifier (matches the ``site_id`` vertex
                     property set during :py:meth:`insert`).

        Returns:
            Dict with keys:
            - ``run_count``   (int): total runs for this site.
            - ``edge_counts`` (dict[str, int]): counts keyed by edge label.
        """
        from gremlin_python.process.graph_traversal import (
            __ as AnonymousT,
        )

        run_count: int = (
            self._g.V()
            .hasLabel("run")
            .has("site_id", site_id)
            .count()
            .next()
        )

        edge_counts_raw: list[Any] = (
            self._g.V()
            .hasLabel("run")
            .has("site_id", site_id)
            .outE()
            .groupCount()
            .by(AnonymousT.label())
            .toList()
        )
        edge_counts: dict[str, int] = {}
        if edge_counts_raw and isinstance(edge_counts_raw[0], dict):
            edge_counts = dict(edge_counts_raw[0])

        return {"run_count": run_count, "edge_counts": edge_counts}
