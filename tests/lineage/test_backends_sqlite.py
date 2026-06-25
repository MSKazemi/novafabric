"""Tests specific to the SQLite lineage backend."""

from __future__ import annotations

from pathlib import Path

import pytest

from novafabric.lineage._types import LineageEdge
from novafabric.lineage.backends.sqlite import SqliteLineageStore
from novafabric.lineage.store import AbstractLineageStore

# ---------------------------------------------------------------------------
# Backend-specific tests
# ---------------------------------------------------------------------------


class TestSqliteLineageStoreInstantiation:
    """Verify construction and default attribute values."""

    def test_requires_db_path(self) -> None:
        with pytest.raises(ValueError, match="db_path is required"):
            SqliteLineageStore()

    def test_default_site_id(self, tmp_path: Path) -> None:
        store = SqliteLineageStore(db_path=tmp_path / "lineage.db")
        assert store.site_id == "local"

    def test_custom_site_id(self, tmp_path: Path) -> None:
        store = SqliteLineageStore(
            db_path=tmp_path / "lineage.db", site_id="cluster-eu-west-1"
        )
        assert store.site_id == "cluster-eu-west-1"

    def test_is_abstract_lineage_store(self, tmp_path: Path) -> None:
        store = SqliteLineageStore(db_path=tmp_path / "lineage.db")
        assert isinstance(store, AbstractLineageStore)


class TestSqliteLineageStoreRoundTrip:
    """End-to-end insert → query tests using a temp-file SQLite database."""

    @pytest.fixture()
    def store(self, tmp_path: Path) -> SqliteLineageStore:
        return SqliteLineageStore(db_path=tmp_path / "lineage.db")

    def test_insert_single_edge(
        self,
        store: SqliteLineageStore,
        sample_edges: list[LineageEdge],
    ) -> None:
        result = store.insert(sample_edges[0])
        assert result is None

    def test_insert_all_and_provenance(
        self,
        store: SqliteLineageStore,
        sample_edges: list[LineageEdge],
    ) -> None:
        for edge in sample_edges:
            store.insert(edge)

        rows = store.provenance(run_id="run-A", depth=3)
        assert isinstance(rows, list)
        assert len(rows) > 0
        for row in rows:
            assert "node_id" in row
            assert "kind" in row
            assert "ref" in row

    def test_blast_radius(
        self,
        store: SqliteLineageStore,
        sample_edges: list[LineageEdge],
    ) -> None:
        for edge in sample_edges:
            store.insert(edge)

        rows = store.blast_radius(run_id="run-D", max_depth=5)
        assert isinstance(rows, list)
        assert len(rows) > 0

    def test_replay_chain(
        self,
        store: SqliteLineageStore,
        sample_edges: list[LineageEdge],
    ) -> None:
        for edge in sample_edges:
            store.insert(edge)

        rows = store.replay_chain(run_id="run-C")
        assert isinstance(rows, list)
        assert len(rows) >= 1

    def test_provenance_unknown_run_returns_empty(
        self,
        store: SqliteLineageStore,
    ) -> None:
        rows = store.provenance(run_id="run-nonexistent", depth=3)
        assert rows == []

    def test_blast_radius_unknown_run_returns_empty(
        self,
        store: SqliteLineageStore,
    ) -> None:
        rows = store.blast_radius(run_id="run-nonexistent", max_depth=3)
        assert rows == []

    def test_replay_chain_unknown_run_returns_empty(
        self,
        store: SqliteLineageStore,
    ) -> None:
        rows = store.replay_chain(run_id="run-nonexistent")
        assert rows == []

    def test_insert_idempotent(
        self,
        store: SqliteLineageStore,
        sample_edges: list[LineageEdge],
    ) -> None:
        edge = sample_edges[0]
        store.insert(edge)
        store.insert(edge)  # must not raise

    def test_time_travel_passthrough(
        self,
        store: SqliteLineageStore,
        sample_edges: list[LineageEdge],
    ) -> None:
        for edge in sample_edges:
            store.insert(edge)

        rows = store.time_travel(ref="run-A", asof="2099-12-31T23:59:59Z", kind="run")
        assert isinstance(rows, list)
