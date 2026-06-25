"""Tests for the PostgresLineageStore stub — verifies NotImplementedError is raised."""

from __future__ import annotations

import pytest

from novafabric.lineage.backends.postgres import PostgresLineageStore
from novafabric.lineage.store import AbstractLineageStore


class TestPostgresLineageStoreStub:
    """Verify the stub raises NotImplementedError for all methods."""

    @pytest.fixture()
    def store(self) -> PostgresLineageStore:
        return PostgresLineageStore()

    def test_is_abstract_lineage_store(self, store: PostgresLineageStore) -> None:
        assert isinstance(store, AbstractLineageStore)

    def test_insert_raises(self, store: PostgresLineageStore, sample_edges: object) -> None:
        with pytest.raises(NotImplementedError, match="Postgres"):
            store.insert(object())  # type: ignore[arg-type]

    def test_provenance_raises(self, store: PostgresLineageStore) -> None:
        with pytest.raises(NotImplementedError, match="Postgres"):
            store.provenance(run_id="x", depth=1)

    def test_blast_radius_raises(self, store: PostgresLineageStore) -> None:
        with pytest.raises(NotImplementedError, match="Postgres"):
            store.blast_radius(run_id="x", max_depth=1)

    def test_replay_chain_raises(self, store: PostgresLineageStore) -> None:
        with pytest.raises(NotImplementedError, match="Postgres"):
            store.replay_chain(run_id="x")
