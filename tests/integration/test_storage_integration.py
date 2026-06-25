"""Integration tests for storage backends (Track S-5).

Parametrised against available backends:
- SQLiteBackend: always runs against a real temp DB.
- PostgresBackend: skipped unless NOVAFABRIC_TEST_POSTGRES_DSN is set.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from novafabric.storage import SQLiteBackend, get_backend
from novafabric.storage._base import StorageInfo

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version VALUES (1);

CREATE TABLE assets (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, asset_type TEXT NOT NULL,
    version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'development',
    spec_json TEXT NOT NULL, git_commit_sha TEXT, created_at TEXT NOT NULL,
    promoted_at TEXT, promoted_by TEXT, forced_promotion INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE eval_results (
    id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, suite_name TEXT NOT NULL,
    passed INTEGER NOT NULL, score_json TEXT, run_at TEXT NOT NULL
);

CREATE TABLE lineage_nodes (
    node_id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
    first_seen_capsule_run_id TEXT, payload TEXT NOT NULL
);

CREATE TABLE lineage_edges (
    edge_id TEXT PRIMARY KEY, edge_type TEXT NOT NULL,
    source_id TEXT NOT NULL, target_id TEXT NOT NULL,
    capsule_run_id TEXT NOT NULL, confidence TEXT,
    created_at TEXT NOT NULL, payload TEXT NOT NULL
);

INSERT INTO assets VALUES (
    'a1', 'integration-model', 'llm', '1.0', 'development',
    '{"name":"integration-model"}', NULL, '2026-01-01T00:00:00Z',
    NULL, NULL, 0
);
"""

_POSTGRES_DSN = os.environ.get("NOVAFABRIC_TEST_POSTGRES_DSN")


def _make_populated_sqlite(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_DDL)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# SQLiteBackend integration tests — always runs
# ---------------------------------------------------------------------------


class TestSQLiteBackendIntegration:
    """Run against a real SQLite database on the filesystem."""

    def test_info_on_missing_db(self, tmp_path: Path) -> None:
        db = tmp_path / "missing.db"
        backend = SQLiteBackend(db)
        info = backend.info()

        assert info.backend == "sqlite"
        assert info.db_path == str(db)
        assert info.schema_version is None
        assert info.row_counts == {}

    def test_info_on_real_db(self, tmp_path: Path) -> None:
        db = tmp_path / "registry.db"
        _make_populated_sqlite(db)

        backend = SQLiteBackend(db)
        info = backend.info()

        assert isinstance(info, StorageInfo)
        assert info.backend == "sqlite"
        assert info.schema_version == 1
        assert info.row_counts["assets"] == 1
        assert info.row_counts["eval_results"] == 0

    def test_get_backend_factory_sqlite(self, tmp_path: Path) -> None:
        backend = get_backend(db_path=tmp_path / "test.db")
        assert isinstance(backend, SQLiteBackend)

    def test_info_db_path_is_string(self, tmp_path: Path) -> None:
        db = tmp_path / "path-check.db"
        _make_populated_sqlite(db)
        info = SQLiteBackend(db).info()
        assert isinstance(info.db_path, str)


# ---------------------------------------------------------------------------
# PostgresBackend integration tests — skipped unless DSN is available
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _POSTGRES_DSN,
    reason="NOVAFABRIC_TEST_POSTGRES_DSN not set — skipping Postgres integration",
)
class TestPostgresBackendIntegration:
    """Run against a live Postgres instance.

    The test database must already exist (created by the CI service container).
    No DDL is run here; we only verify that the backend can connect and that
    ``info()`` returns a coherent ``StorageInfo`` even on an empty database.
    """

    def test_info_returns_storage_info(self) -> None:
        assert _POSTGRES_DSN is not None  # narrowed for type checker
        from novafabric.storage._postgres import PostgresBackend

        backend = PostgresBackend(_POSTGRES_DSN)
        info = backend.info()

        assert isinstance(info, StorageInfo)
        assert info.backend == "postgres"
        assert info.db_path is None
        # schema_version may be None (empty DB) or an int — both are valid
        assert info.schema_version is None or isinstance(info.schema_version, int)

    def test_get_backend_factory_postgres(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert _POSTGRES_DSN is not None
        monkeypatch.setenv("NOVAFABRIC_POSTGRES_DSN", _POSTGRES_DSN)
        from novafabric.storage._postgres import PostgresBackend

        backend = get_backend(backend="postgres")
        assert isinstance(backend, PostgresBackend)
