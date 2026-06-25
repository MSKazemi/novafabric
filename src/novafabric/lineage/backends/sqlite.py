"""SQLite backend — wraps :class:`~novafabric.lineage._store.LineageStore`."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from novafabric.lineage._store import LineageStore
from novafabric.lineage._types import LineageEdge
from novafabric.lineage.store import AbstractLineageStore, NodeRow


class SqliteLineageStore(AbstractLineageStore):
    """``AbstractLineageStore`` adapter backed by SQLite via ``LineageStore``."""

    def __init__(
        self,
        db_path: Path | None = None,
        site_id: str = "local",
    ) -> None:
        if db_path is None:
            raise ValueError(
                "db_path is required for SqliteLineageStore; "
                "pass a Path to a file to avoid polluting the developer database."
            )
        self._db_path = db_path
        self._local = threading.local()
        self.site_id = site_id

    @property
    def _delegate(self) -> LineageStore:
        """Return a per-thread LineageStore instance."""
        if not hasattr(self._local, "store"):
            self._local.store = LineageStore(db_path=self._db_path)
        store: LineageStore = self._local.store
        return store

    # ------------------------------------------------------------------
    # AbstractLineageStore interface
    # ------------------------------------------------------------------

    def insert(self, edge: LineageEdge) -> None:
        """Persist *edge* (and implied nodes) idempotently."""
        self._delegate.insert_edge(edge)

    def provenance(self, run_id: str, depth: int) -> list[NodeRow]:
        """Return nodes reachable forward from *run_id* up to *depth* hops."""
        return self._delegate.provenance(ref=run_id, kind="run", depth=depth)

    def blast_radius(self, run_id: str, max_depth: int) -> list[NodeRow]:
        """Return upstream nodes that could have influenced *run_id*."""
        return self._delegate.blast_radius(ref=run_id, kind="run", depth=max_depth)

    def replay_chain(self, run_id: str) -> list[NodeRow]:
        """Return ordered replay-chain nodes anchored at *run_id*."""
        return self._delegate.replay_chain(run_id=run_id)

    # ------------------------------------------------------------------
    # Passthrough helpers
    # ------------------------------------------------------------------

    def time_travel(
        self, ref: str, asof: str, kind: str | None = None
    ) -> list[dict[str, Any]]:
        """Delegate to the underlying store's ``time_travel`` method."""
        return self._delegate.time_travel(ref=ref, asof=asof, kind=kind)
