"""L20: ``NOVAFABRIC_DB_PATH`` names the registry DB only; the metadata DB has its own variable.

``_paths.metadata_db_path()`` and ``registry_db_path()`` both read ``NOVAFABRIC_DB_PATH``.
Set for the registry as documented, the default ``nova db upgrade`` (metadata track) ran
the MetadataStore alembic tree against ``registry.db`` — while the runtime
``SQLiteMetadataStore`` never read the variable and kept using ``metadata.db``. The
metadata DB is now named by ``NOVAFABRIC_METADATA_DB_PATH`` (default
``$NOVAFABRIC_HOME/metadata.db``), for the runtime and the migration alike.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from novafabric import _paths
from novafabric.cli.main import app

runner = CliRunner()


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def test_registry_and_metadata_paths_are_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(tmp_path / "custom-registry.db"))
    monkeypatch.delenv("NOVAFABRIC_METADATA_DB_PATH", raising=False)
    assert _paths.registry_db_path() == tmp_path / "custom-registry.db"
    assert _paths.metadata_db_path() == tmp_path / "metadata.db"

    monkeypatch.setenv("NOVAFABRIC_METADATA_DB_PATH", str(tmp_path / "custom-meta.db"))
    assert _paths.metadata_db_path() == tmp_path / "custom-meta.db"
    assert _paths.registry_db_path() == tmp_path / "custom-registry.db"


def test_runtime_store_default_follows_the_metadata_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.metadata_store.sqlite import _default_db_path

    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path))
    monkeypatch.setenv("NOVAFABRIC_METADATA_DB_PATH", str(tmp_path / "m.db"))
    assert _default_db_path() == tmp_path / "m.db"


def test_db_upgrade_never_migrates_the_registry_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.registry.store import get_connection, init_schema

    registry = tmp_path / "registry-elsewhere.db"
    conn = get_connection(registry)
    init_schema(conn)
    conn.close()
    before = _tables(registry)

    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(registry))
    monkeypatch.delenv("NOVAFABRIC_METADATA_DB_PATH", raising=False)
    result = runner.invoke(app, ["db", "upgrade"], env={"COLUMNS": "300"})

    assert result.exit_code == 0, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert _tables(registry) == before, "the metadata track wrote into the registry DB"
    meta = tmp_path / "home" / "metadata.db"
    assert "alembic_version" in _tables(meta)
    # The operator who relied on the old meaning is told, not silently redirected.
    assert "NOVAFABRIC_METADATA_DB_PATH" in result.output


def test_db_upgrade_targets_the_metadata_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "meta" / "custom.db"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVAFABRIC_METADATA_DB_PATH", str(target))
    monkeypatch.delenv("NOVAFABRIC_DB_PATH", raising=False)
    result = runner.invoke(app, ["db", "upgrade"], env={"COLUMNS": "300"})
    assert result.exit_code == 0, result.output
    assert "alembic_version" in _tables(target)
    assert not (tmp_path / "home" / "metadata.db").exists()
    assert "NOVAFABRIC_DB_PATH" not in result.output


def test_programmatic_upgrade_ignores_the_registry_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from novafabric.metadata_store.cli import run_alembic_upgrade

    registry = tmp_path / "reg.db"
    monkeypatch.setenv("NOVAFABRIC_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("NOVAFABRIC_DB_PATH", str(registry))
    monkeypatch.delenv("NOVAFABRIC_METADATA_DB_PATH", raising=False)
    ok, message = run_alembic_upgrade("sqlite", revision="head")
    assert ok, message
    assert not registry.exists()
    assert "alembic_version" in _tables(tmp_path / "home" / "metadata.db")
