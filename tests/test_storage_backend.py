"""Tests for src/novafabric/storage/ — StorageBackend abstraction."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from novafabric.storage import SQLiteBackend, get_backend
from novafabric.storage._base import StorageInfo

# ---------------------------------------------------------------------------
# SQLiteBackend
# ---------------------------------------------------------------------------


def test_sqlite_backend_missing_db(tmp_path: Path) -> None:
    db = tmp_path / "missing.db"
    backend = SQLiteBackend(db)
    info = backend.info()

    assert info.backend == "sqlite"
    assert info.db_path == str(db)
    assert info.schema_version is None
    assert info.row_counts == {}


def test_sqlite_backend_empty_db(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.close()

    backend = SQLiteBackend(db)
    info = backend.info()

    assert info.backend == "sqlite"
    assert info.schema_version is None
    assert info.row_counts == {}


def test_sqlite_backend_initialised_db(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
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
        INSERT INTO assets
            VALUES ('a1','m','llm','1','development','{}',NULL,'2026-01-01',NULL,NULL,0);
        """
    )
    conn.commit()
    conn.close()

    backend = SQLiteBackend(db)
    info = backend.info()

    assert info.backend == "sqlite"
    assert info.schema_version == 1
    assert info.row_counts["assets"] == 1
    assert info.row_counts["eval_results"] == 0
    assert info.row_counts["lineage_nodes"] == 0
    assert info.row_counts["lineage_edges"] == 0


def test_sqlite_backend_row_count_missing_table(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY); "
        "INSERT INTO schema_version VALUES (1);"
    )
    conn.commit()
    conn.close()

    backend = SQLiteBackend(db)
    info = backend.info()

    # Tables that don't exist are silently omitted from row_counts
    assert "assets" not in info.row_counts
    assert info.schema_version == 1


# ---------------------------------------------------------------------------
# get_backend factory
# ---------------------------------------------------------------------------


def test_get_backend_defaults_to_sqlite(tmp_path: Path) -> None:
    backend = get_backend(db_path=tmp_path / "x.db")
    assert isinstance(backend, SQLiteBackend)


def test_get_backend_env_var_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_BACKEND", "sqlite")
    backend = get_backend(db_path=tmp_path / "x.db")
    assert isinstance(backend, SQLiteBackend)


def test_get_backend_unknown_raises() -> None:
    with pytest.raises(ValueError, match="Unknown backend"):
        get_backend(backend="duckdb")


def test_get_backend_postgres_no_dsn_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOVAFABRIC_POSTGRES_DSN", raising=False)
    with pytest.raises(ValueError, match="no DSN"):
        get_backend(backend="postgres")


# ---------------------------------------------------------------------------
# StorageInfo dataclass
# ---------------------------------------------------------------------------


def test_storage_info_defaults() -> None:
    info = StorageInfo(backend="sqlite", db_path="/tmp/x.db", schema_version=1)
    assert info.row_counts == {}
    assert info.migration_pending is None


# ---------------------------------------------------------------------------
# SQLiteBackend — alembic migration check paths
# ---------------------------------------------------------------------------


def test_sqlite_migration_pending_alembic_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY); "
        "INSERT INTO schema_version VALUES (1);"
    )
    conn.commit()
    conn.close()

    # Remove alembic from sys.modules so the import check fails
    monkeypatch.setitem(sys.modules, "alembic", None)  # type: ignore[misc]

    backend = SQLiteBackend(db)
    info = backend.info()
    # alembic not installed → migration_pending is None
    assert info.migration_pending is None


def test_sqlite_migration_pending_alembic_installed_no_version_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY); "
        "INSERT INTO schema_version VALUES (1);"
    )
    conn.commit()
    conn.close()

    # Inject a fake alembic module so the import check passes
    fake_alembic = MagicMock()
    monkeypatch.setitem(sys.modules, "alembic", fake_alembic)

    backend = SQLiteBackend(db)
    info = backend.info()
    # alembic installed but alembic_version table absent → pending = True
    assert info.migration_pending is True


def test_sqlite_migration_pending_alembic_version_table_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY); "
        "INSERT INTO schema_version VALUES (1); "
        "CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY); "
        "INSERT INTO alembic_version VALUES ('s0001');"
    )
    conn.commit()
    conn.close()

    fake_alembic = MagicMock()
    monkeypatch.setitem(sys.modules, "alembic", fake_alembic)

    backend = SQLiteBackend(db)
    info = backend.info()
    # alembic installed and version_num row present → pending = False
    assert info.migration_pending is False


# ---------------------------------------------------------------------------
# PostgresBackend — ImportError path (psycopg not installed)
# ---------------------------------------------------------------------------


def test_sqlite_schema_version_empty_table(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    # Table exists but has no rows → MAX(version) returns NULL → schema_version is None
    conn.executescript("CREATE TABLE schema_version (version INTEGER PRIMARY KEY);")
    conn.commit()
    conn.close()

    backend = SQLiteBackend(db)
    info = backend.info()
    assert info.schema_version is None


def test_postgres_backend_no_psycopg_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "psycopg", None)  # type: ignore[misc]

    from novafabric.storage._postgres import PostgresBackend

    backend = PostgresBackend("postgresql://localhost/test")
    with pytest.raises(RuntimeError, match="novafabric\\[server\\]"):
        backend.info()


def _make_fake_psycopg(
    schema_version_row: object, count_rows: list[object]
) -> MagicMock:
    """Build a fake psycopg module with a context-manager connection."""
    call_results = [schema_version_row, *count_rows]
    fake_cursor = MagicMock()
    fake_cursor.fetchone.side_effect = call_results

    fake_conn = MagicMock()
    fake_conn.__enter__ = MagicMock(return_value=fake_conn)
    fake_conn.__exit__ = MagicMock(return_value=False)
    fake_conn.execute.return_value = fake_cursor

    fake_psycopg = MagicMock()
    fake_psycopg.connect.return_value = fake_conn
    return fake_psycopg


def test_postgres_backend_info_with_mock_psycopg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_psycopg = _make_fake_psycopg([1], [[0], [0], [0], [0]])
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    from novafabric.storage._postgres import PostgresBackend

    backend = PostgresBackend("postgresql://localhost/test")
    info = backend.info()

    assert info.backend == "postgres"
    assert info.schema_version == 1
    assert info.db_path is None
    assert info.migration_pending is None


def test_postgres_backend_schema_version_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_psycopg = _make_fake_psycopg(None, [[0], [0], [0], [0]])
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    from novafabric.storage._postgres import PostgresBackend

    backend = PostgresBackend("postgresql://localhost/test")
    info = backend.info()

    assert info.schema_version is None


def test_postgres_backend_query_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_cursor = MagicMock()
    fake_cursor.fetchone.side_effect = RuntimeError("connection reset")

    fake_conn = MagicMock()
    fake_conn.__enter__ = MagicMock(return_value=fake_conn)
    fake_conn.__exit__ = MagicMock(return_value=False)
    fake_conn.execute.return_value = fake_cursor

    fake_psycopg = MagicMock()
    fake_psycopg.connect.return_value = fake_conn

    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

    from novafabric.storage._postgres import PostgresBackend

    backend = PostgresBackend("postgresql://localhost/test")
    info = backend.info()

    assert info.schema_version is None
    assert info.row_counts == {}


def test_get_backend_postgres_with_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOVAFABRIC_POSTGRES_DSN", "postgresql://localhost/test")
    from novafabric.storage._postgres import PostgresBackend

    backend = get_backend(backend="postgres")
    assert isinstance(backend, PostgresBackend)


# ---------------------------------------------------------------------------
# Postgres backend (skipped unless NOVAFABRIC_TEST_POSTGRES_DSN is set)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("NOVAFABRIC_TEST_POSTGRES_DSN"),
    reason="NOVAFABRIC_TEST_POSTGRES_DSN not set",
)
def test_postgres_backend_info() -> None:
    dsn = os.environ["NOVAFABRIC_TEST_POSTGRES_DSN"]
    backend = get_backend(backend="postgres", postgres_dsn=dsn)
    info = backend.info()
    assert info.backend == "postgres"
    assert info.db_path is None
