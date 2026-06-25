"""Public ABC for lineage backend implementations."""

from __future__ import annotations

import abc
from typing import Any

from novafabric.lineage._types import LineageEdge

# NodeRow is the common dict shape returned by all query methods.
NodeRow = dict[str, Any]  # must contain: node_id (str), kind (str), ref (str)


class AbstractLineageStore(abc.ABC):
    """ABC for lineage storage backends; subclass and implement all methods."""

    @abc.abstractmethod
    def insert(self, edge: LineageEdge) -> None:
        """Persist *edge* (and implied nodes) idempotently."""

    @abc.abstractmethod
    def provenance(self, run_id: str, depth: int) -> list[NodeRow]:
        """Return nodes reachable forward from *run_id* up to *depth* hops."""

    @abc.abstractmethod
    def blast_radius(self, run_id: str, max_depth: int) -> list[NodeRow]:
        """Return upstream nodes that could have influenced *run_id*."""

    @abc.abstractmethod
    def replay_chain(self, run_id: str) -> list[NodeRow]:
        """Return ordered replay-chain nodes anchored at *run_id*."""
