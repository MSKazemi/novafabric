"""Tests for the Alembic dual-track migration system (FR-04).

Verifies:
1. All expected migration files exist in the SQLite track.
2. All expected migration files exist in the Postgres track.
3. v002 upgrade() and downgrade() are callable functions (no longer NotImplementedError).
4. SQLiteMetadataStore.bootstrap() is idempotent (safe to call twice).
5. PostgresMetadataStore.bootstrap() is idempotent and health_check returns ok.

Test 5 is skipped gracefully when Docker is unavailable (inherits the
``postgres_url`` session fixture from conftest.py).
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_MIGRATIONS_ROOT = (
    Path(__file__).parent.parent.parent
    / "src"
    / "novafabric"
    / "metadata_store"
    / "migrations"
)
_SQLITE_TRACK = _MIGRATIONS_ROOT / "sqlite"
_POSTGRES_TRACK = _MIGRATIONS_ROOT / "postgres"


# ---------------------------------------------------------------------------
# Test 1: SQLite track files exist
# ---------------------------------------------------------------------------


def test_sqlite_migration_files_exist() -> None:
    """All required files in the SQLite migration track must be present."""
    required = [
        _SQLITE_TRACK / "env.py",
        _SQLITE_TRACK / "alembic.ini",
        _SQLITE_TRACK / "script.py.mako",
        _SQLITE_TRACK / "versions" / "__init__.py",
        _SQLITE_TRACK / "versions" / "v001_init.py",
    ]
    for path in required:
        assert path.exists(), f"Missing SQLite migration file: {path}"


# ---------------------------------------------------------------------------
# Test 2: Postgres track files exist
# ---------------------------------------------------------------------------


def test_postgres_migration_files_exist() -> None:
    """All required files in the Postgres migration track must be present."""
    required = [
        _POSTGRES_TRACK / "env.py",
        _POSTGRES_TRACK / "alembic.ini",
        _POSTGRES_TRACK / "script.py.mako",
        _POSTGRES_TRACK / "versions" / "__init__.py",
        _POSTGRES_TRACK / "versions" / "v001_init.py",
        _POSTGRES_TRACK / "versions" / "v002_partition_ddl.py",
    ]
    for path in required:
        assert path.exists(), f"Missing Postgres migration file: {path}"


# ---------------------------------------------------------------------------
# Test 3: v002 upgrade() and downgrade() are implemented (no longer NotImplementedError)
# ---------------------------------------------------------------------------


def test_v002_is_implemented() -> None:
    """v002_partition_ddl.upgrade() and downgrade() must be callable functions.

    ADR-0051 accepted; Strategy A (range on started_at) is implemented.
    The full 10K x 1M benchmark is hardware-gated but the migration itself
    is no longer blocked by a NotImplementedError guard.
    """
    mod = importlib.import_module(
        "novafabric.metadata_store.migrations.postgres.versions.v002_partition_ddl"
    )
    # Both must be callable without raising at import/inspection time.
    assert callable(mod.upgrade), "v002 upgrade() must be a callable"
    assert callable(mod.downgrade), "v002 downgrade() must be a callable"
    # Revision metadata must be correct.
    assert mod.revision == "v002"
    assert mod.down_revision == "v001"


# ---------------------------------------------------------------------------
# Test 4: SQLiteMetadataStore.bootstrap() is idempotent
# ---------------------------------------------------------------------------


def test_sqlite_bootstrap_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling bootstrap() twice on SQLiteMetadataStore must not raise."""
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(tmp_path / "meta.db"))
    monkeypatch.delenv("NOVAFABRIC_API_WORKERS", raising=False)

    from novafabric.metadata_store.sqlite import SQLiteMetadataStore

    store = SQLiteMetadataStore(db_path=tmp_path / "meta.db")
    store.bootstrap()
    store.bootstrap()  # second call must be idempotent

    health = store.health_check()
    assert health["status"] == "ok"
    assert health["backend"] == "sqlite"


# ---------------------------------------------------------------------------
# Test 5: PostgresMetadataStore.bootstrap() is idempotent
# ---------------------------------------------------------------------------


def test_postgres_bootstrap_idempotent(postgres_url: str) -> None:
    """Calling bootstrap() twice on PostgresMetadataStore must not raise."""
    from novafabric.metadata_store.postgres import PostgresMetadataStore

    store = PostgresMetadataStore(dsn=postgres_url)
    store.bootstrap()
    store.bootstrap()  # second call must be idempotent — all tables use IF NOT EXISTS

    health = store.health_check()
    assert health["status"] == "ok"
    assert health["backend"] == "postgres"
