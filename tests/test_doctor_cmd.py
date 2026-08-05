"""Tests for src/novafabric/cli/doctor.py — nova doctor command."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from _help_assert import assert_flag_in_help
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
    assert_flag_in_help(result, "--check-storage")


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


def test_doctor_check_storage_migration_pending(tmp_path: Path) -> None:
    # ADR-0211: an unstamped (init_schema-bootstrapped) DB is "pending" —
    # the comparator, not a table-existence heuristic, decides now.
    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY); "
        "INSERT INTO schema_version VALUES (1);"
    )
    conn.commit()
    conn.close()

    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "pending" in result.output
    # ADR-0211 D5: the hint must name the track that fixes THIS database.
    assert "--track registry" in result.output.replace("\n", "")


def test_doctor_check_storage_migration_up_to_date(tmp_path: Path) -> None:
    # ADR-0211: "up to date" now means stamped AT the script head — a stale
    # stamp (e.g. s0001 with a newer head) correctly reports pending instead.
    from novafabric.migrations.registry_track import script_head

    head = script_head("sqlite")
    assert head is not None, "source checkout must resolve the sqlite head"

    db = tmp_path / "registry.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER PRIMARY KEY); "
        "INSERT INTO schema_version VALUES (1); "
        "CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY); "
        f"INSERT INTO alembic_version VALUES ('{head}');"
    )
    conn.commit()
    conn.close()

    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "up to date" in result.output


def test_doctor_check_storage_migration_stale_stamp_is_pending(
    tmp_path: Path,
) -> None:
    # A DB stamped behind head must report pending (the pre-ADR-0211 code
    # reported "up to date" for ANY stamp — the bug this slice fixes).
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

    result = runner.invoke(
        app, ["doctor", "--check-storage", "--db-path", str(db)]
    )
    assert result.exit_code == 0
    assert "pending" in result.output


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


# ---------------------------------------------------------------------------
# nova doctor --check-scheduler (OQ-06, PAR-ADR-003)
# ---------------------------------------------------------------------------

_SCHEDULER_ENV_VARS = [
    "SLURM_JOB_ID",
    "SLURM_JOBID",
    "SLURM_EXPORT_ENV",
    "TORCHELASTIC_RUN_ID",
    "OMPI_COMM_WORLD_RANK",
    "RAY_WORLD_SIZE",
    "KUBERNETES_SERVICE_HOST",
    "NOVAFABRIC_GLOBAL_RUN_ID",
]


@pytest.fixture(autouse=True)
def _clean_scheduler_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _SCHEDULER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_doctor_check_scheduler_no_scheduler_is_ok() -> None:
    result = runner.invoke(app, ["doctor", "--check-scheduler"])
    assert result.exit_code == 0
    assert "OK" in result.output


def test_doctor_check_scheduler_slurm_export_none_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    monkeypatch.setenv("SLURM_EXPORT_ENV", "NONE")

    result = runner.invoke(app, ["doctor", "--check-scheduler"])

    assert result.exit_code == 1
    assert "Mismatch detected" in result.output
    assert "SLURM_EXPORT_ENV" in result.output


def test_doctor_check_scheduler_with_contract_vars_present_is_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    monkeypatch.setenv("NOVAFABRIC_GLOBAL_RUN_ID", "01ARZ3NDEKTSV4RRFFQ69G5FAV")

    result = runner.invoke(app, ["doctor", "--check-scheduler"])

    assert result.exit_code == 0
    assert "OK" in result.output


def test_doctor_no_flags_mentions_check_scheduler() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert_flag_in_help(result, "--check-scheduler")
