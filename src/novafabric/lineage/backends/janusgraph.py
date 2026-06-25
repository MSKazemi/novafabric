"""JanusGraph lineage backend — distributed graph DB (v3 tier).

Requires JanusGraph container:
    docker run -p 8182:8182 janusgraph/janusgraph:1.0.0

Integration tests gated by NOVA_INTEGRATION=1.
Install the optional extra: uv add "novafabric[janusgraph]"
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from novafabric.lineage._types import LineageEdge
from novafabric.lineage.store import AbstractLineageStore, NodeRow

if TYPE_CHECKING:  # pragma: no cover
    # Type stubs only — not imported at runtime unless gremlinpython is present.
    pass

_MISSING = (
    "gremlinpython not installed; run: uv add gremlinpython  "
    "or: uv add 'novafabric[janusgraph]'"
)


class JanusGraphLineageStore(AbstractLineageStore):
    """JanusGraph distributed backend using Gremlin Python (v3 tier).

    Connection is deferred (lazy) — the actual Gremlin WebSocket is not
    opened until the first operation, so instantiation is always cheap.

    Requires JanusGraph container running at *gremlin_endpoint*:
        docker run -p 8182:8182 janusgraph/janusgraph:1.0.0

    Integration tests gated by NOVA_INTEGRATION=1.
    """

    def __init__(
        self,
        gremlin_endpoint: str = "ws://localhost:8182/gremlin",
        site_id: str = "local",
    ) -> None:
        # Eager import-check so callers get a clear error immediately.
        try:
            import gremlin_python  # noqa: F401
        except ImportError as exc:
            raise ImportError(_MISSING) from exc

        self._endpoint = gremlin_endpoint
        self._site_id = site_id
        self._g: Any = None
        self._conn: Any = None

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def _connect(self) -> None:
        """Open the Gremlin WebSocket connection (idempotent)."""
        if self._g is not None:
            return
        from gremlin_python.driver.driver_remote_connection import (
            DriverRemoteConnection,
        )
        from gremlin_python.process.anonymous_traversal import (
            traversal,
        )

        self._conn = DriverRemoteConnection(self._endpoint, "g")
        self._g = traversal().withRemote(self._conn)

    def close(self) -> None:
        """Close the Gremlin connection gracefully."""
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
                self._g = None

    # ------------------------------------------------------------------
    # AbstractLineageStore interface
    # ------------------------------------------------------------------

    def insert(self, edge: LineageEdge) -> None:
        """Persist *edge* (and implied run vertices) idempotently.

        Upserts both endpoint vertices, then adds a directed edge with the
        correct *edge_type* label.  Vertex upsert uses Gremlin's
        ``fold/coalesce/unfold`` idiom for safe concurrent writes.
        """
        self._connect()
        from gremlin_python.process.graph_traversal import (
            __ as AnonymousT,
        )

        src_run_id = edge.source.get("run_id", "")
        tgt_run_id = edge.target.get("run_id", "")

        g = self._g

        # Upsert source vertex.
        (
            g.V()
            .has("run_id", src_run_id)
            .fold()
            .coalesce(
                AnonymousT.unfold(),
                AnonymousT.addV("run")
                .property("run_id", src_run_id)
                .property("site_id", self._site_id),
            )
            .next()
        )

        # Upsert target vertex.
        (
            g.V()
            .has("run_id", tgt_run_id)
            .fold()
            .coalesce(
                AnonymousT.unfold(),
                AnonymousT.addV("run")
                .property("run_id", tgt_run_id)
                .property("site_id", self._site_id),
            )
            .next()
        )

        # Add directed edge with the edge_type as label.
        (
            g.V()
            .has("run_id", src_run_id)
            .addE(edge.edge_type)
            .to(AnonymousT.V().has("run_id", tgt_run_id))
            .property("edge_id", edge.edge_id)
            .property("capsule_run_id", edge.capsule_run_id)
            .next()
        )

    def provenance(self, run_id: str, depth: int) -> list[NodeRow]:
        """Return nodes reachable *forward* from *run_id* up to *depth* hops."""
        self._connect()
        from gremlin_python.process.graph_traversal import (
            __ as AnonymousT,
        )

        results: list[Any] = (
            self._g.V()
            .has("run_id", run_id)
            .repeat(AnonymousT.out())
            .times(depth)
            .dedup()
            .project("node_id", "kind", "ref")
            .by("run_id")
            .by(AnonymousT.label())
            .by("run_id")
            .toList()
        )
        return [dict(r) for r in results]

    def blast_radius(self, run_id: str, max_depth: int) -> list[NodeRow]:
        """Return upstream nodes that could have influenced *run_id*."""
        self._connect()
        from gremlin_python.process.graph_traversal import (
            __ as AnonymousT,
        )

        results: list[Any] = (
            self._g.V()
            .has("run_id", run_id)
            .repeat(AnonymousT.in_())
            .times(max_depth)
            .dedup()
            .project("node_id", "kind", "ref")
            .by("run_id")
            .by(AnonymousT.label())
            .by("run_id")
            .toList()
        )
        return [dict(r) for r in results]

    def replay_chain(self, run_id: str) -> list[NodeRow]:
        """Return nodes linked via *replayed_from* edges anchored at *run_id*."""
        self._connect()
        from gremlin_python.process.graph_traversal import (
            __ as AnonymousT,
        )

        results: list[Any] = (
            self._g.V()
            .has("run_id", run_id)
            .repeat(AnonymousT.out("replayed_from"))
            .emit()
            .project("node_id", "kind", "ref")
            .by("run_id")
            .by(AnonymousT.label())
            .by("run_id")
            .toList()
        )
        return [dict(r) for r in results]
