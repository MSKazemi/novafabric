"""Tests for ``nova migrate-to-postgres`` (Track S-1).

Unit tests use a SQLite fixture and a mock psycopg module so they never
require a real Postgres instance.

The integration test at the bottom is skipped unless
``NOVAFABRIC_TEST_POSTGRES_DSN`` is set.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from novafabric.cli.main import app
from novafabric.storage._migration import (
    MigrationSummary,
    TableMigrationResult,
    run_migration,
)

# ---------------------------------------------------------------------------
# Helpers — build a minimal SQLite database with known row counts
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);
INSERT INTO schema_version VALUES (1);

CREATE TABLE assets (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, asset_type TEXT NOT NULL,
    version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'development',
    spec_json TEXT NOT NULL, git_commit_sha TEXT, created_at TEXT NOT NULL,
    promoted_at TEXT, promoted_by TEXT, forced_promotion INTEGER NOT NULL DEFAULT 0,
    UNIQUE(name, version)
);

CREATE TABLE eval_results (
    id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, suite_name TEXT NOT NULL,
    passed INTEGER NOT NULL, score_json TEXT, run_at TEXT NOT NULL
);

CREATE TABLE lineage_nodes (
    node_id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
    first_seen_capsule_run_id TEXT, payload TEXT NOT NULL,
    UNIQUE(kind, ref)
);

CREATE TABLE lineage_edges (
    edge_id TEXT PRIMARY KEY, edge_type TEXT NOT NULL,
    source_id TEXT NOT NULL, target_id TEXT NOT NULL,
    capsule_run_id TEXT NOT NULL, confidence TEXT,
    created_at TEXT NOT NULL, payload TEXT NOT NULL
);
"""

_ASSET_ROW = (
    "a1", "model-a", "llm", "1.0", "development",
    '{"name":"model-a","version":"1.0","asset_type":"llm"}',
    None, "2026-01-01T00:00:00Z", None, None, 0,
)
_ASSET_ROW2 = (
    "a2", "model-b", "llm", "1.0", "development",
    '{"name":"model-b","version":"1.0","asset_type":"llm"}',
    None, "2026-01-02T00:00:00Z", None, None, 0,
)
_EVAL_ROW = ("e1", "a1", "suite-1", 1, None, "2026-01-01T01:00:00Z")
_NODE_ROW = ("node-1", "run", "run-abc", None, '{"run_id":"run-abc"}')
_EDGE_ROW = (
    "edge-1", "used", "node-1", "node-1",
    "cap-1", None, "2026-01-01T00:00:00Z", "{}",
)


def _make_sqlite_db(path: Path, *, with_rows: bool = True) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_DDL)
    if with_rows:
        conn.execute(
            "INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?,?,?)", _ASSET_ROW
        )
        conn.execute(
            "INSERT INTO eval_results VALUES (?,?,?,?,?,?)", _EVAL_ROW
        )
        conn.execute(
            "INSERT INTO lineage_nodes VALUES (?,?,?,?,?)", _NODE_ROW
        )
        conn.execute(
            "INSERT INTO lineage_edges VALUES (?,?,?,?,?,?,?,?)", _EDGE_ROW
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Mock psycopg factory
# ---------------------------------------------------------------------------

def _make_mock_psycopg(
    *,
    connect_raises: Exception | None = None,
    rowcount: int = 1,
) -> MagicMock:
    """Return a fake ``psycopg`` module whose connection supports execute()."""
    fake_psycopg = MagicMock(name="psycopg")

    if connect_raises is not None:
        fake_psycopg.connect.side_effect = connect_raises
        return fake_psycopg

    # Cursor returned by execute()
    fake_cursor = MagicMock()
    fake_cursor.rowcount = rowcount
    # COUNT(*) query returns (n,)
    fake_cursor.fetchone.return_value = (rowcount,)

    fake_conn = MagicMock()
    fake_conn.__enter__ = MagicMock(return_value=fake_conn)
    fake_conn.__exit__ = MagicMock(return_value=False)
    fake_conn.execute.return_value = fake_cursor

    fake_psycopg.connect.return_value = fake_conn
    return fake_psycopg


# ---------------------------------------------------------------------------
# _migration module — unit tests
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_returns_sqlite_counts_no_pg_connection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)

        # psycopg must not be imported at all during a dry run
        monkeypatch.setitem(sys.modules, "psycopg", None)  # type: ignore[misc]

        summary = run_migration(db, target_dsn="", dry_run=True)

        assert summary.dry_run is True
        assert summary.sqlite_counts["assets"] == 1
        assert summary.sqlite_counts["eval_results"] == 1
        assert summary.sqlite_counts["lineage_nodes"] == 1
        assert summary.sqlite_counts["lineage_edges"] == 1
        assert summary.postgres_counts == {}
        assert summary.verification_passed is None
        assert summary.exit_code() == 0

    def test_dry_run_empty_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db, with_rows=False)
        monkeypatch.setitem(sys.modules, "psycopg", None)  # type: ignore[misc]

        summary = run_migration(db, target_dsn="", dry_run=True)

        assert all(v == 0 for v in summary.sqlite_counts.values())
        assert summary.exit_code() == 0

    def test_dry_run_produces_skipped_status_in_table_results(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)
        monkeypatch.setitem(sys.modules, "psycopg", None)  # type: ignore[misc]

        summary = run_migration(db, target_dsn="", dry_run=True)

        for result in summary.table_results:
            assert result.status == "skipped"
            assert result.rows_written == 0


class TestHappyPath:
    def test_migration_writes_rows_and_verifies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)

        fake_psycopg = _make_mock_psycopg(rowcount=1)
        monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

        summary = run_migration(db, target_dsn="postgresql://localhost/test")

        assert summary.dry_run is False
        # All four tables processed
        assert len(summary.table_results) == 4
        for result in summary.table_results:
            assert result.status == "ok"
        # Verification: postgres_counts >= sqlite_counts (mock returns 1 for all)
        assert summary.verification_passed is True
        assert summary.exit_code() == 0

    def test_verification_passes_when_pg_has_more_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Postgres may have more rows (from other sources); still passes."""
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)  # 1 row per table in SQLite

        # Mock Postgres reporting 5 rows per table (already had rows)
        fake_psycopg = _make_mock_psycopg(rowcount=5)
        monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

        summary = run_migration(db, target_dsn="postgresql://localhost/test")
        assert summary.verification_passed is True

    def test_verification_fails_when_pg_has_fewer_rows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)  # 1 row per table in SQLite

        # Mock Postgres COUNT(*) returning 0 (write silently failed)
        fake_psycopg = _make_mock_psycopg(rowcount=0)
        monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

        summary = run_migration(db, target_dsn="postgresql://localhost/test")
        assert summary.verification_passed is False
        assert summary.exit_code() == 1


class TestPartialRetry:
    """Idempotency: a second run must not duplicate rows."""

    def test_on_conflict_do_nothing_semantics(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)

        insert_calls: list[tuple[str, ...]] = []

        fake_cursor = MagicMock()
        fake_cursor.rowcount = 0  # ON CONFLICT DO NOTHING → no row inserted
        fake_cursor.fetchone.return_value = (1,)  # COUNT(*) → 1 existing row

        fake_conn = MagicMock()
        fake_conn.__enter__ = MagicMock(return_value=fake_conn)
        fake_conn.__exit__ = MagicMock(return_value=False)

        def execute_side_effect(sql: str, *args: Any, **kwargs: Any) -> MagicMock:
            if sql.strip().upper().startswith("INSERT"):
                insert_calls.append((sql,))
            return fake_cursor

        fake_conn.execute.side_effect = execute_side_effect

        fake_psycopg = MagicMock()
        fake_psycopg.connect.return_value = fake_conn
        monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

        summary = run_migration(db, target_dsn="postgresql://localhost/test")

        # All INSERT statements must include ON CONFLICT DO NOTHING
        for (sql,) in insert_calls:
            assert "ON CONFLICT DO NOTHING" in sql.upper(), (
                f"Expected ON CONFLICT DO NOTHING in: {sql}"
            )
        # rows_written is 0 (nothing new to add) but verification still passes
        # because COUNT(*) returns 1 >= sqlite_counts[table] == 1
        assert summary.verification_passed is True


class TestConnectionError:
    def test_connect_raises_connection_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)

        class FakePsycopgError(Exception):
            pass

        fake_psycopg = _make_mock_psycopg(
            connect_raises=FakePsycopgError("connection refused")
        )
        monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

        with pytest.raises(ConnectionError, match="connection refused"):
            run_migration(db, target_dsn="postgresql://bad-host/test")

    def test_psycopg_not_installed_raises_runtime_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)

        monkeypatch.setitem(sys.modules, "psycopg", None)  # type: ignore[misc]

        with pytest.raises(RuntimeError, match="novafabric\\[server\\]"):
            run_migration(db, target_dsn="postgresql://localhost/test")


class TestLog:
    def test_log_written_for_dry_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        log_path = tmp_path / "migration.jsonl"
        _make_sqlite_db(db)
        monkeypatch.setitem(sys.modules, "psycopg", None)  # type: ignore[misc]

        run_migration(db, target_dsn="", dry_run=True, log_path=log_path)

        assert log_path.exists()
        lines = log_path.read_text().splitlines()
        assert len(lines) == 4  # one per table
        for line in lines:
            record = json.loads(line)
            assert "table" in record
            assert "rows_read" in record
            assert "rows_written" in record
            assert "status" in record

    def test_log_written_for_real_migration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        log_path = tmp_path / "migration.jsonl"
        _make_sqlite_db(db)

        fake_psycopg = _make_mock_psycopg(rowcount=1)
        monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

        run_migration(
            db,
            target_dsn="postgresql://localhost/test",
            log_path=log_path,
        )

        lines = log_path.read_text().splitlines()
        assert len(lines) == 4
        for line in lines:
            record = json.loads(line)
            assert record["status"] == "ok"

    def test_log_appends_on_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        log_path = tmp_path / "migration.jsonl"
        _make_sqlite_db(db)
        monkeypatch.setitem(sys.modules, "psycopg", None)  # type: ignore[misc]

        run_migration(db, target_dsn="", dry_run=True, log_path=log_path)
        run_migration(db, target_dsn="", dry_run=True, log_path=log_path)

        lines = log_path.read_text().splitlines()
        # Two runs × 4 tables = 8 lines
        assert len(lines) == 8


class TestTableMigrationResult:
    def test_as_jsonl_record_ok(self) -> None:
        r = TableMigrationResult(
            table="assets", rows_read=3, rows_written=3, status="ok"
        )
        record = json.loads(r.as_jsonl_record())
        assert record == {
            "table": "assets",
            "rows_read": 3,
            "rows_written": 3,
            "status": "ok",
        }

    def test_as_jsonl_record_with_error(self) -> None:
        r = TableMigrationResult(
            table="assets",
            rows_read=0,
            rows_written=0,
            status="error",
            error="insert failed",
        )
        record = json.loads(r.as_jsonl_record())
        assert record["error"] == "insert failed"

    def test_as_jsonl_record_no_error_key_when_none(self) -> None:
        r = TableMigrationResult(
            table="assets", rows_read=1, rows_written=1, status="ok"
        )
        record = json.loads(r.as_jsonl_record())
        assert "error" not in record


class TestMigrationSummaryExitCode:
    def test_exit_code_0_on_success(self) -> None:
        s = MigrationSummary(dry_run=False)
        s.verification_passed = True
        assert s.exit_code() == 0

    def test_exit_code_1_on_verification_failure(self) -> None:
        s = MigrationSummary(dry_run=False)
        s.verification_passed = False
        assert s.exit_code() == 1

    def test_exit_code_0_on_dry_run(self) -> None:
        s = MigrationSummary(dry_run=True)
        # verification_passed is None in dry-run mode
        assert s.exit_code() == 0


# ---------------------------------------------------------------------------
# CLI tests (Typer runner)
# ---------------------------------------------------------------------------

runner = CliRunner()


class TestMigrateToPostgresCLI:
    def test_help_text(self) -> None:
        result = runner.invoke(app, ["migrate-to-postgres", "--help"])
        assert result.exit_code == 0
        assert "source" in result.output.lower()
        assert "target" in result.output.lower()
        assert "dry-run" in result.output.lower()

    def test_missing_source_exits_2(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "migrate-to-postgres",
                "--source",
                str(tmp_path / "nonexistent.db"),
                "--target",
                "postgresql://localhost/test",
            ],
        )
        assert result.exit_code == 2

    def test_missing_target_no_env_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)
        monkeypatch.delenv("NOVAFABRIC_POSTGRES_DSN", raising=False)

        result = runner.invoke(
            app,
            ["migrate-to-postgres", "--source", str(db)],
        )
        assert result.exit_code == 2
        assert "required" in result.output.lower() or "--target" in result.output

    def test_dry_run_no_postgres_needed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)
        monkeypatch.setitem(sys.modules, "psycopg", None)  # type: ignore[misc]
        monkeypatch.delenv("NOVAFABRIC_POSTGRES_DSN", raising=False)

        result = runner.invoke(
            app,
            [
                "migrate-to-postgres",
                "--source",
                str(db),
                "--dry-run",
            ],
        )
        assert result.exit_code == 0
        assert "Dry run" in result.output

    def test_dry_run_shows_table_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)
        monkeypatch.setitem(sys.modules, "psycopg", None)  # type: ignore[misc]
        monkeypatch.delenv("NOVAFABRIC_POSTGRES_DSN", raising=False)

        result = runner.invoke(
            app,
            ["migrate-to-postgres", "--source", str(db), "--dry-run"],
        )
        assert "assets" in result.output
        assert "eval_results" in result.output

    def test_connection_error_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)

        class FakePgError(Exception):
            pass

        fake_psycopg = _make_mock_psycopg(connect_raises=FakePgError("refused"))
        monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

        result = runner.invoke(
            app,
            [
                "migrate-to-postgres",
                "--source",
                str(db),
                "--target",
                "postgresql://bad/test",
            ],
        )
        assert result.exit_code == 2
        assert "Connection error" in result.output or "Error" in result.output

    def test_psycopg_not_installed_exits_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)
        monkeypatch.setitem(sys.modules, "psycopg", None)  # type: ignore[misc]

        result = runner.invoke(
            app,
            [
                "migrate-to-postgres",
                "--source",
                str(db),
                "--target",
                "postgresql://localhost/test",
            ],
        )
        assert result.exit_code == 2

    def test_successful_migration_exits_0(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)

        fake_psycopg = _make_mock_psycopg(rowcount=1)
        monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

        result = runner.invoke(
            app,
            [
                "migrate-to-postgres",
                "--source",
                str(db),
                "--target",
                "postgresql://localhost/test",
            ],
        )
        assert result.exit_code == 0
        assert "PASS" in result.output

    def test_verification_failure_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)

        # Postgres returns 0 rows for COUNT(*) — mismatch vs SQLite's 1
        fake_psycopg = _make_mock_psycopg(rowcount=0)
        monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

        result = runner.invoke(
            app,
            [
                "migrate-to-postgres",
                "--source",
                str(db),
                "--target",
                "postgresql://localhost/test",
            ],
        )
        assert result.exit_code == 1
        assert "FAIL" in result.output

    def test_log_flag_creates_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        log_path = tmp_path / "migrate.jsonl"
        _make_sqlite_db(db)
        monkeypatch.setitem(sys.modules, "psycopg", None)  # type: ignore[misc]
        monkeypatch.delenv("NOVAFABRIC_POSTGRES_DSN", raising=False)

        result = runner.invoke(
            app,
            [
                "migrate-to-postgres",
                "--source",
                str(db),
                "--dry-run",
                "--log",
                str(log_path),
            ],
        )
        assert result.exit_code == 0
        assert log_path.exists()
        lines = log_path.read_text().splitlines()
        assert len(lines) == 4

    def test_dsn_password_redacted_in_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = tmp_path / "registry.db"
        _make_sqlite_db(db)

        fake_psycopg = _make_mock_psycopg(rowcount=1)
        monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

        result = runner.invoke(
            app,
            [
                "migrate-to-postgres",
                "--source",
                str(db),
                "--target",
                "postgresql://user:s3cr3t@localhost/test",
            ],
        )
        assert "s3cr3t" not in result.output


# ---------------------------------------------------------------------------
# Tests for missing tables in SQLite (graceful degradation)
# ---------------------------------------------------------------------------


class TestMissingTables:
    def test_migration_with_only_assets_table(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Databases that only have the assets table should migrate cleanly."""
        db = tmp_path / "partial.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE assets (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, asset_type TEXT NOT NULL,
                version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'development',
                spec_json TEXT NOT NULL, git_commit_sha TEXT,
                created_at TEXT NOT NULL, promoted_at TEXT,
                promoted_by TEXT, forced_promotion INTEGER NOT NULL DEFAULT 0,
                UNIQUE(name, version)
            );
            INSERT INTO assets VALUES
                ('x1','a','llm','1','development','{}',NULL,'2026-01-01',NULL,NULL,0);
            """
        )
        conn.commit()
        conn.close()

        monkeypatch.setitem(sys.modules, "psycopg", None)  # type: ignore[misc]

        summary = run_migration(db, target_dsn="", dry_run=True)
        assert summary.sqlite_counts["assets"] == 1
        assert summary.sqlite_counts["eval_results"] == 0
        assert summary.sqlite_counts["lineage_nodes"] == 0


# ---------------------------------------------------------------------------
# Integration test (skipped unless NOVAFABRIC_TEST_POSTGRES_DSN is set)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("NOVAFABRIC_TEST_POSTGRES_DSN"),
    reason="NOVAFABRIC_TEST_POSTGRES_DSN not set",
)
def test_migration_integration_round_trip(tmp_path: Path) -> None:
    """Full round-trip: SQLite → Postgres; row counts verified."""
    import psycopg  # type: ignore[import-untyped]

    dsn = os.environ["NOVAFABRIC_TEST_POSTGRES_DSN"]
    db = tmp_path / "registry.db"
    _make_sqlite_db(db)

    # Clean up any leftover tables from a previous test run
    with psycopg.connect(dsn) as conn:
        for tbl in ("lineage_edges", "lineage_nodes", "eval_results", "assets"):
            try:
                conn.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")  # noqa: S608
            except Exception:  # noqa: BLE001
                pass

    summary = run_migration(db, target_dsn=dsn)

    assert summary.verification_passed is True
    assert summary.exit_code() == 0
    assert summary.sqlite_counts["assets"] == 1
    assert summary.postgres_counts["assets"] >= 1


@pytest.mark.skipif(
    not os.environ.get("NOVAFABRIC_TEST_POSTGRES_DSN"),
    reason="NOVAFABRIC_TEST_POSTGRES_DSN not set",
)
def test_migration_integration_idempotent(tmp_path: Path) -> None:
    """Re-running the migration does not duplicate rows."""
    import psycopg  # type: ignore[import-untyped]

    dsn = os.environ["NOVAFABRIC_TEST_POSTGRES_DSN"]
    db = tmp_path / "idempotent.db"
    _make_sqlite_db(db)

    # Clean up
    with psycopg.connect(dsn) as conn:
        for tbl in ("lineage_edges", "lineage_nodes", "eval_results", "assets"):
            try:
                conn.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")  # noqa: S608
            except Exception:  # noqa: BLE001
                pass

    # First run
    s1 = run_migration(db, target_dsn=dsn)
    assert s1.verification_passed is True

    # Second run — must not duplicate
    s2 = run_migration(db, target_dsn=dsn)
    assert s2.verification_passed is True

    # Row counts in Postgres after second run must equal counts after first run
    assert s2.postgres_counts == s1.postgres_counts
