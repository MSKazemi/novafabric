"""FR-05 scale tests: 100K-row SQLite seed, timing gate, and Postgres migration.

Three test groups:

1. ``test_sqlite_100k_rows_insert``        — no Docker required.
   Writes 20 tenants × 5000 runs = 100 000 rows to a tmp SQLite db and asserts
   the count.  Also validates a 1% random checksum (UUID format check).

2. ``test_sqlite_100k_insert_timing``      — no Docker required.
   Same seed, measures wall-clock time, asserts < 30 s (FR-05 performance gate).
   Reports p99 per-row insert time.

3. ``TestPostgresMigration100k``           — NOVA_INTEGRATION=1 + Docker required.
   Migrates the 100K-row SQLite db to Postgres via the CLI and verifies:
   - exact row count parity
   - 1% random checksum: every sampled run_id exists in the source SQLite db
"""
from __future__ import annotations

import os
import random
import re
import sqlite3
import time
import uuid
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

N_TENANTS = 20
N_RUNS_PER_TENANT = 5000
EXPECTED_ROWS = N_TENANTS * N_RUNS_PER_TENANT  # 100_000
SAMPLE_SIZE = max(1, EXPECTED_ROWS // 100)      # 1 % → 1 000 rows
TIMING_LIMIT_S = 30.0                           # FR-05 performance gate
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _seed_sqlite_100k(db_path: Path) -> list[str]:
    """Seed 100K rows into *db_path*; return the list of all inserted run_ids."""
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

    all_run_ids: list[str] = []
    batch: list[tuple[str, str, str, str]] = []

    for _ in range(N_TENANTS):
        tenant_id = str(uuid.uuid4())
        for _ in range(N_RUNS_PER_TENANT):
            run_id = str(uuid.uuid4())
            batch.append((run_id, tenant_id, "run.started", "2026-05-19T00:00:00Z"))
            all_run_ids.append(run_id)

    conn.executemany(
        "INSERT OR IGNORE INTO runs (run_id, tenant_id, event_type, started_at) "
        "VALUES (?, ?, ?, ?)",
        batch,
    )
    conn.commit()
    conn.close()
    return all_run_ids


# ---------------------------------------------------------------------------
# Test 1: count + 1 % checksum (SQLite only, no Docker)
# ---------------------------------------------------------------------------


def test_sqlite_100k_rows_insert(tmp_path: Path) -> None:
    """100K rows can be written to SQLite without errors (FR-05 scale lower bound).

    Also runs a 1% random checksum: every sampled run_id must be a valid UUID
    and must be retrievable by primary key from the same database.
    """
    db_path = tmp_path / "scale_100k.db"
    all_run_ids = _seed_sqlite_100k(db_path)

    # --- row-count assertion ------------------------------------------------
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
    assert row is not None
    actual_count: int = row[0]
    assert actual_count == EXPECTED_ROWS, (
        f"Expected {EXPECTED_ROWS} rows in SQLite, found {actual_count}"
    )

    # --- 1 % checksum: UUID format + existence by PK ----------------------
    sample = random.sample(all_run_ids, SAMPLE_SIZE)
    mismatches: list[str] = []
    for run_id in sample:
        # Format check
        if not _UUID_RE.match(run_id):
            mismatches.append(f"bad UUID format: {run_id!r}")
            continue
        # Existence check (PK lookup)
        row = conn.execute(
            "SELECT 1 FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            mismatches.append(f"run_id not found in db: {run_id!r}")

    conn.close()

    assert not mismatches, (
        f"1% checksum failed on {len(mismatches)}/{SAMPLE_SIZE} sampled rows:\n"
        + "\n".join(mismatches[:10])
    )


# ---------------------------------------------------------------------------
# Test 2: timing gate (SQLite only, no Docker)
# ---------------------------------------------------------------------------


def test_sqlite_100k_insert_timing(tmp_path: Path) -> None:
    """100K SQLite inserts must complete in < 30 s (FR-05 performance gate).

    Also computes a p99 per-row insert time.  Note: this test uses *executemany*
    in a single batch (same as ``_seed_sqlite_100k``).  The p99 is estimated
    from the total wall-clock time divided by EXPECTED_ROWS.
    """
    db_path = tmp_path / "timing_100k.db"

    t0 = time.monotonic()
    _seed_sqlite_100k(db_path)
    elapsed = time.monotonic() - t0

    per_row_ms = (elapsed / EXPECTED_ROWS) * 1000
    # p99 estimate (single-batch insert; all rows share the same transaction)
    p99_ms = per_row_ms  # lower-bound: single batch means p99 ≈ mean

    assert elapsed < TIMING_LIMIT_S, (
        f"100K SQLite insert took {elapsed:.2f}s — exceeds FR-05 gate of {TIMING_LIMIT_S}s"
    )

    # Informational — surfaced in pytest -s output
    print(
        f"\n[FR-05 timing] 100K rows in {elapsed:.3f}s  "
        f"({per_row_ms:.4f} ms/row  p99≈{p99_ms:.4f} ms)"
    )


# ---------------------------------------------------------------------------
# Test 3: Postgres migration at 100K rows (NOVA_INTEGRATION=1 gate)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("NOVA_INTEGRATION"),
    reason="Requires NOVA_INTEGRATION=1 + Docker Postgres",
)
class TestPostgresMigration100k:
    """Migrate 100K SQLite rows to Postgres; verify count and 1% checksum (FR-05)."""

    def test_postgres_migration_100k_rows(
        self, tmp_path: Path, postgres_url: str
    ) -> None:
        """Seed 20 × 5000 rows, run migrate-to-postgres CLI, assert parity + checksum."""
        from typer.testing import CliRunner

        from novafabric.metadata_store.cli import metadata_db_app

        db_path = tmp_path / "scale_pg_100k.db"
        all_run_ids = _seed_sqlite_100k(db_path)

        # Verify source count
        sqlite_conn = sqlite3.connect(db_path)
        src_row = sqlite_conn.execute("SELECT COUNT(*) FROM runs").fetchone()
        assert src_row is not None
        src_count: int = src_row[0]
        sqlite_run_id_set = {
            row[0]
            for row in sqlite_conn.execute("SELECT run_id FROM runs").fetchall()
        }
        # ``postgres_url`` is a SESSION-scoped container shared by every test in
        # tests/metadata_store, so `runs` also holds other tests' rows. Every
        # assertion below must therefore be scoped to the tenants THIS test
        # seeded (fresh uuid4 per invocation) — otherwise the 1% sample draws
        # foreign rows and the checksum fails for a reason that has nothing to
        # do with the migration. That is exactly what made this test fail on
        # every nightly run: ~5 of 1000 sampled rows belonged to other tests.
        sqlite_tenant_ids = [
            row[0]
            for row in sqlite_conn.execute(
                "SELECT DISTINCT tenant_id FROM runs"
            ).fetchall()
        ]
        sqlite_conn.close()
        assert len(sqlite_tenant_ids) == N_TENANTS
        assert src_count == EXPECTED_ROWS

        # Run migration CLI
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
        assert result.exit_code == 0, (
            f"CLI migrate-to-postgres failed (exit {result.exit_code}):\n{result.output}"
        )

        # --- row-count parity in Postgres ----------------------------------
        import psycopg  # noqa: PLC0415

        with psycopg.connect(postgres_url) as pg_conn:
            pg_row = pg_conn.execute(
                "SELECT COUNT(*) FROM runs WHERE tenant_id = ANY(%s)",
                (sqlite_tenant_ids,),
            ).fetchone()
            assert pg_row is not None
            pg_count: int = pg_row[0]
            # Read on a NEW connection, deliberately: the migration CLI once
            # reported "PASS — row counts verified" from its own uncommitted
            # transaction while every row was rolled back at close. Include the
            # CLI's own output here — without it this assertion says only
            # "0 rows" and gives no hint that the CLI believed it succeeded.
            # Scoped to this test's tenants, so it can be EXACT: `>=` was only
            # ever needed to tolerate the other tests sharing this container.
            assert pg_count == EXPECTED_ROWS, (
                f"Postgres has {pg_count} rows for this test's {N_TENANTS} "
                f"tenants; expected exactly {EXPECTED_ROWS}.\n"
                f"CLI reported:\n{result.output}"
            )

            # --- 1 % checksum: sampled run_ids exist in source SQLite ------
            rows = pg_conn.execute(
                "SELECT run_id FROM runs WHERE tenant_id = ANY(%s) "
                "ORDER BY random() LIMIT %s",
                (sqlite_tenant_ids, SAMPLE_SIZE),
            ).fetchall()

        pg_sample_ids = [row[0] for row in rows]
        assert len(pg_sample_ids) == SAMPLE_SIZE, (
            f"Expected {SAMPLE_SIZE} sampled rows from Postgres, got {len(pg_sample_ids)}"
        )

        missing_in_source: list[str] = []
        for run_id in pg_sample_ids:
            # UUID format check
            if not _UUID_RE.match(str(run_id)):
                missing_in_source.append(f"bad UUID format: {run_id!r}")
                continue
            # Existence check against source SQLite set
            if str(run_id) not in sqlite_run_id_set:
                missing_in_source.append(
                    f"run_id {run_id!r} in Postgres but not in source SQLite"
                )

        assert not missing_in_source, (
            f"1% checksum failed on {len(missing_in_source)}/{SAMPLE_SIZE} rows:\n"
            + "\n".join(missing_in_source[:10])
        )

        # Ensure source file was not deleted
        assert db_path.exists(), "Source SQLite file was deleted during migration!"

        # Keep linter happy — all_run_ids confirms seed succeeded
        assert len(all_run_ids) == EXPECTED_ROWS
