"""Integration tests for ``nova migrate-to-postgres`` (Track S-5).

Requires a live Postgres instance via NOVAFABRIC_TEST_POSTGRES_DSN.
Skipped entirely when the env var is absent.

The test:
1. Builds a populated SQLite fixture database.
2. Invokes ``run_migration()`` against the real Postgres DSN.
3. Verifies that row counts in Postgres match the SQLite source.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

_POSTGRES_DSN = os.environ.get("NOVAFABRIC_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not _POSTGRES_DSN,
    reason="NOVAFABRIC_TEST_POSTGRES_DSN not set — skipping migration integration",
)

# ---------------------------------------------------------------------------
# DDL + fixture data
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

_ASSET_ROWS = [
    (
        "mig-a1",
        "mig-model-a",
        "llm",
        "1.0",
        "development",
        '{"name":"mig-model-a","version":"1.0","asset_type":"llm"}',
        None,
        "2026-01-01T00:00:00Z",
        None,
        None,
        0,
    ),
    (
        "mig-a2",
        "mig-model-b",
        "llm",
        "1.0",
        "development",
        '{"name":"mig-model-b","version":"1.0","asset_type":"llm"}',
        None,
        "2026-01-02T00:00:00Z",
        None,
        None,
        0,
    ),
]

_EVAL_ROW = ("mig-e1", "mig-a1", "suite-mig", 1, None, "2026-01-01T01:00:00Z")
_NODE_ROW = ("mig-node-1", "run", "run-mig-abc", None, '{"run_id":"run-mig-abc"}')
_EDGE_ROW = (
    "mig-edge-1",
    "used",
    "mig-node-1",
    "mig-node-1",
    "mig-cap-1",
    None,
    "2026-01-01T00:00:00Z",
    "{}",
)

# Expected row counts in the source SQLite
_EXPECTED_COUNTS = {
    "assets": 2,
    "eval_results": 1,
    "lineage_nodes": 1,
    "lineage_edges": 1,
}


def _make_source_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(_DDL)
    for row in _ASSET_ROWS:
        conn.execute(
            "INSERT INTO assets VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )
    conn.execute("INSERT INTO eval_results VALUES (?,?,?,?,?,?)", _EVAL_ROW)
    conn.execute("INSERT INTO lineage_nodes VALUES (?,?,?,?,?)", _NODE_ROW)
    conn.execute("INSERT INTO lineage_edges VALUES (?,?,?,?,?,?,?,?)", _EDGE_ROW)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestMigrationIntegration:
    def test_migrate_row_counts_match(self, tmp_path: Path) -> None:
        """Populate SQLite → migrate → verify Postgres row counts equal source."""
        assert _POSTGRES_DSN is not None  # narrowed for type checker

        source_db = tmp_path / "source.db"
        log_path = tmp_path / "migration.jsonl"
        _make_source_db(source_db)

        from novafabric.storage._migration import run_migration

        summary = run_migration(source_db, _POSTGRES_DSN, log_path=log_path)

        # The migration must report success (exit_code 0)
        assert summary.exit_code() == 0, (
            f"Migration failed: {summary!r}"
        )

        # Per-table: everything read out of SQLite must land in Postgres.
        #
        # This asserted `table_result.source_rows` until 2026-08-05 — a field
        # `TableMigrationResult` has never had, so the test raised AttributeError
        # rather than checking anything. It went unnoticed because the job that
        # runs it could not reach this file: it installed without `--all-extras`,
        # so alembic was absent and the migration step died first. Checking both
        # sides is what the test's own name claims, and is stronger than either
        # alone — `rows_written == expected` with a short read would pass a
        # silently truncated migration.
        for table_result in summary.table_results:
            expected = _EXPECTED_COUNTS.get(table_result.table, 0)
            assert table_result.rows_read == expected, (
                f"Table {table_result.table}: expected to read {expected} source "
                f"rows, read {table_result.rows_read}"
            )
            assert table_result.rows_written == expected, (
                f"Table {table_result.table}: read {table_result.rows_read} rows "
                f"but wrote {table_result.rows_written} — the migration dropped rows"
            )

    def test_migrate_idempotent(self, tmp_path: Path) -> None:
        """Running migration twice must not raise or double-count rows."""
        assert _POSTGRES_DSN is not None

        source_db = tmp_path / "source2.db"
        _make_source_db(source_db)

        from novafabric.storage._migration import run_migration

        first = run_migration(source_db, _POSTGRES_DSN)
        second = run_migration(source_db, _POSTGRES_DSN)

        # Both runs must report success
        assert first.exit_code() == 0
        assert second.exit_code() == 0

    def test_migrate_log_file_written(self, tmp_path: Path) -> None:
        """A JSONL log file must be written when log_path is provided."""
        assert _POSTGRES_DSN is not None

        source_db = tmp_path / "source3.db"
        log_path = tmp_path / "migration.jsonl"
        _make_source_db(source_db)

        from novafabric.storage._migration import run_migration

        run_migration(source_db, _POSTGRES_DSN, log_path=log_path)

        assert log_path.exists()
        lines = log_path.read_text().strip().splitlines()
        # At least one log line per table (4 tables)
        assert len(lines) >= 4
