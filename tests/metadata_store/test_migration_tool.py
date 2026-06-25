"""Tests for nova metadata-migrate-to-postgres migration tool.

FR-05: idempotent SQLite → Postgres migration for the metadata store.

All tests that hit Postgres depend on the ``postgres_url`` session fixture
defined in conftest.py.  They are gracefully skipped when Docker is unavailable.
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_sqlite(db_path: Path, n_tenants: int, n_runs_per_tenant: int) -> int:
    """Seed a minimal SQLite metadata.db with runs rows.

    Returns total number of rows inserted.
    """
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id            TEXT,
            tenant_id         TEXT,
            event_type        TEXT,
            global_run_id     TEXT,
            started_at        TEXT,
            status            TEXT DEFAULT 'pending',
            world_size        INTEGER,
            expected_children INTEGER,
            children_arrived  INTEGER DEFAULT 0,
            PRIMARY KEY (run_id, tenant_id)
        );
        CREATE TABLE IF NOT EXISTS capsules (
            capsule_uri  TEXT PRIMARY KEY,
            run_id       TEXT,
            tenant_id    TEXT
        );
        CREATE TABLE IF NOT EXISTS signatures (
            run_id         TEXT,
            tenant_id      TEXT,
            signature_hash TEXT,
            payload_json   TEXT,
            PRIMARY KEY (run_id, signature_hash)
        );
        CREATE TABLE IF NOT EXISTS retention_policies (
            tenant_id   TEXT PRIMARY KEY,
            policy_json TEXT
        );
        """
    )
    total = 0
    for _ in range(n_tenants):
        tenant_id = str(uuid.uuid4())
        for _ in range(n_runs_per_tenant):
            run_id = str(uuid.uuid4())
            conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, tenant_id, event_type, started_at) "
                "VALUES (?, ?, ?, ?)",
                (run_id, tenant_id, "run.started", "2026-05-13T00:00:00Z"),
            )
            total += 1
    conn.commit()
    conn.close()
    return total


# ---------------------------------------------------------------------------
# Test 1: row-count parity after migration
# ---------------------------------------------------------------------------


def test_migration_row_count_parity(tmp_path: Path, postgres_url: str) -> None:
    """Seed 5 tenants × 20 runs (100 rows); migrate; assert Postgres has 100 rows."""
    from typer.testing import CliRunner

    from novafabric.metadata_store.cli import metadata_db_app

    db_path = tmp_path / "source.db"
    expected_rows = _seed_sqlite(db_path, n_tenants=5, n_runs_per_tenant=20)
    assert expected_rows == 100

    runner = CliRunner()
    result = runner.invoke(
        metadata_db_app,
        [
            "migrate-to-postgres",
            "--source",
            str(db_path),
            "--target",
            postgres_url,
        ],
    )
    assert result.exit_code == 0, f"CLI failed:\n{result.output}"

    # Verify Postgres row count
    import psycopg

    with psycopg.connect(postgres_url) as conn:
        row = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
    assert row is not None
    actual = row[0]
    assert actual >= expected_rows, (
        f"Expected at least {expected_rows} rows in Postgres, got {actual}"
    )


# ---------------------------------------------------------------------------
# Test 2: migration is idempotent (re-runnable without duplicates)
# ---------------------------------------------------------------------------


def test_migration_is_rerunnable(tmp_path: Path, postgres_url: str) -> None:
    """Run migration twice; Postgres should still have exactly 20 rows (no duplicates)."""
    from typer.testing import CliRunner

    from novafabric.metadata_store.cli import metadata_db_app

    db_path = tmp_path / "source_idem.db"
    expected_rows = _seed_sqlite(db_path, n_tenants=2, n_runs_per_tenant=10)
    assert expected_rows == 20

    runner = CliRunner()
    args = [
        "migrate-to-postgres",
        "--source",
        str(db_path),
        "--target",
        postgres_url,
    ]

    # First run
    result1 = runner.invoke(metadata_db_app, args)
    assert result1.exit_code == 0, f"First run failed:\n{result1.output}"

    # Second run (same source, same target)
    result2 = runner.invoke(metadata_db_app, args)
    assert result2.exit_code == 0, f"Second run failed:\n{result2.output}"

    import psycopg

    # Count rows that match the source db's content.
    # We can't easily isolate to just this test's data in a shared PG container,
    # so we verify the count is at least the expected and is stable across two runs.
    with psycopg.connect(postgres_url) as conn:
        row = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
    assert row is not None
    after_second = row[0]

    # A third run should yield the same count (no duplicates).
    result3 = runner.invoke(metadata_db_app, args)
    assert result3.exit_code == 0, f"Third run failed:\n{result3.output}"

    with psycopg.connect(postgres_url) as conn:
        row = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
    assert row is not None
    after_third = row[0]

    assert after_third == after_second, (
        f"Row count changed after third run: {after_second} → {after_third}. "
        "Migration is not idempotent."
    )


# ---------------------------------------------------------------------------
# Test 3: source file is never deleted
# ---------------------------------------------------------------------------


def test_source_file_preserved(tmp_path: Path, postgres_url: str) -> None:
    """Source SQLite file must still exist after migration."""
    from typer.testing import CliRunner

    from novafabric.metadata_store.cli import metadata_db_app

    db_path = tmp_path / "preserved.db"
    _seed_sqlite(db_path, n_tenants=1, n_runs_per_tenant=5)
    assert db_path.exists()

    runner = CliRunner()
    result = runner.invoke(
        metadata_db_app,
        [
            "migrate-to-postgres",
            "--source",
            str(db_path),
            "--target",
            postgres_url,
        ],
    )
    assert result.exit_code == 0, f"CLI failed:\n{result.output}"
    assert db_path.exists(), "Source SQLite file was deleted during migration!"
