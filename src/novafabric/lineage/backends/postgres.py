"""Postgres lineage backend stub — not yet implemented."""

from __future__ import annotations

from novafabric.lineage._types import LineageEdge
from novafabric.lineage.store import AbstractLineageStore, NodeRow

_MSG = (
    "Full Postgres (psycopg) backend not yet implemented; "
    "use SqliteLineageStore for local mode"
)


class PostgresLineageStore(AbstractLineageStore):
    """Stub for the future psycopg-backed Postgres lineage store."""

    def insert(self, edge: LineageEdge) -> None:
        """Not implemented."""
        raise NotImplementedError(_MSG)

    def provenance(self, run_id: str, depth: int) -> list[NodeRow]:
        """Not implemented."""
        raise NotImplementedError(_MSG)

    def blast_radius(self, run_id: str, max_depth: int) -> list[NodeRow]:
        """Not implemented."""
        raise NotImplementedError(_MSG)

    def replay_chain(self, run_id: str) -> list[NodeRow]:
        """Not implemented."""
        raise NotImplementedError(_MSG)
