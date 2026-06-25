"""Apache AGE lineage backend stub — conditional on Postgres + AGE extension.

Ships as experimental pending benchmark confirmation of p99 < 500ms at 10M edges
(Phase 6 B-7). See design/adr/0053-lineagestore-v2-tiering.md for the gate condition.
"""

from __future__ import annotations

from novafabric.lineage._types import LineageEdge
from novafabric.lineage.store import AbstractLineageStore, NodeRow

_MSG_INIT = (
    "AGELineageStore requires PostgreSQL with Apache AGE extension. "
    "Install AGE and implement psycopg integration. "
    "Gate: benchmark p99 depth-5 < 500ms at 10M edges (Phase 6 B-7)."
)
_MSG_METHOD = "AGELineageStore not yet implemented"


class AGELineageStore(AbstractLineageStore):
    """Apache AGE graph extension backend (conditional — requires Postgres + AGE extension).

    Ships as experimental pending benchmark confirmation of p99 < 500ms at 10M edges
    (Phase 6 B-7). See design/adr/0053-lineagestore-v2-tiering.md for gate condition.
    """

    def __init__(self, dsn: str, site_id: str = "local") -> None:
        raise NotImplementedError(_MSG_INIT)

    def insert(self, edge: LineageEdge) -> None:
        """Not implemented."""
        raise NotImplementedError(_MSG_METHOD)

    def provenance(self, run_id: str, depth: int) -> list[NodeRow]:
        """Not implemented."""
        raise NotImplementedError(_MSG_METHOD)

    def blast_radius(self, run_id: str, max_depth: int) -> list[NodeRow]:
        """Not implemented."""
        raise NotImplementedError(_MSG_METHOD)

    def replay_chain(self, run_id: str) -> list[NodeRow]:
        """Not implemented."""
        raise NotImplementedError(_MSG_METHOD)
