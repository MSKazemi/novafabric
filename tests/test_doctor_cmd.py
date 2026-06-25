"""Tests for src/novafabric/cli/doctor.py — nova doctor command."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.storage._base import StorageInfo

runner = CliRunner()


# ---------------------------------------------------------------------------
# nova doctor (no flags)
# ---------------------------------------------------------------------------


def test_doctor_no_flags_prints_hint() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "--check-storage" in result.output


# ---------------------------------------------------------------------------
# nova doctor --check-storage (SQLite path)
# ---------------------------------------------------------------------------


def test_doctor_check_storage_sqlite_initialised(tmp_path: Path) -> None:
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
            promoted_at TEXT, promoted_by TEXT,
            forced_promotion INTEGER NOT NULL DEFAULT 0
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
        """
    )
    conn.commit()
    conn.close()

    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "sqlite" in result.output
    assert "Schema version" in result.output
    assert "assets" in result.output


def test_doctor_check_storage_missing_db(tmp_path: Path) -> None:
    db = tmp_path / "nonexistent.db"
    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "sqlite" in result.output


def test_doctor_check_storage_shows_row_counts(tmp_path: Path) -> None:
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
            promoted_at TEXT, promoted_by TEXT,
            forced_promotion INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO assets
            VALUES ('a1','m','llm','1','development','{}',NULL,'2026-01-01',NULL,NULL,0);
        INSERT INTO assets
            VALUES ('a2','n','llm','1','development','{}',NULL,'2026-01-01',NULL,NULL,0);
        """
    )
    conn.commit()
    conn.close()

    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "assets" in result.output
    assert "2" in result.output  # 2 asset rows


def test_doctor_check_storage_migration_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from unittest.mock import MagicMock

    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY); "
        "INSERT INTO schema_version VALUES (1);"
    )
    conn.commit()
    conn.close()

    fake_alembic = MagicMock()
    monkeypatch.setitem(sys.modules, "alembic", fake_alembic)

    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "pending" in result.output


def test_doctor_check_storage_migration_up_to_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from unittest.mock import MagicMock

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

    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "up to date" in result.output


def test_doctor_check_storage_sqlite_uninitialised_schema(tmp_path: Path) -> None:
    db = tmp_path / "registry.db"
    # DB exists but has no tables at all
    conn = sqlite3.connect(str(db))
    conn.close()

    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "not initialised" in result.output


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------


def test_doctor_check_storage_backend_error(tmp_path: Path) -> None:
    with patch(
        "novafabric.cli.doctor.get_backend",
        side_effect=ValueError("bad config"),
    ):
        result = runner.invoke(app, ["doctor", "--check-storage"])
    assert result.exit_code == 1
    assert "bad config" in result.output


# ---------------------------------------------------------------------------
# _print_storage_report — branch coverage
# ---------------------------------------------------------------------------


def test_print_storage_report_postgres_no_db_path() -> None:
    from novafabric.cli.doctor import _print_storage_report

    info = StorageInfo(
        backend="postgres",
        db_path=None,
        schema_version=2,
        row_counts={"assets": 10},
        migration_pending=None,
    )
    # Just assert it doesn't raise; output goes to the module-level console
    _print_storage_report(info)


def test_print_storage_report_empty_rows_with_path(tmp_path: Path) -> None:
    from novafabric.cli.doctor import _print_storage_report

    info = StorageInfo(
        backend="sqlite",
        db_path=str(tmp_path / "x.db"),
        schema_version=None,
        row_counts={},
        migration_pending=None,
    )
    _print_storage_report(info)
