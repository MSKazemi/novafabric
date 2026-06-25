"""Partition DDL — Strategy A: quarterly RANGE partitions on ``started_at``.

Background
----------
Partitioning the ``runs`` table by ``started_at`` (quarterly) enables
partition pruning on time-range queries ("show me recent runs"), which is
the dominant query shape in NovaFabric.  RLS already provides tenant
isolation; the partition key is chosen for query performance, not
multi-tenancy.

Strategy A: PARTITION BY RANGE (started_at)
-------------------------------------------
- Quarterly partitions covering 2024-Q1 through 2027-Q4 (16 total).
- The composite PRIMARY KEY (run_id, started_at) includes the partition key
  as required by Postgres declarative partitioning.
- RLS (ENABLE + FORCE ROW LEVEL SECURITY, ``tenant_isolation`` policy) is
  re-applied to the partitioned table after rename.
- Grants for ``novafabric_app`` and ``novafabric_migrator`` are re-applied.

Upgrade
-------
1. Create ``runs_partitioned`` (same schema, PARTITION BY RANGE (started_at)).
2. Create 16 quarterly child tables: ``runs_y2024q1`` ... ``runs_y2027q4``.
3. Copy data: INSERT INTO runs_partitioned SELECT ... FROM runs ON CONFLICT DO NOTHING.
4. DROP TABLE runs (old un-partitioned table).
5. RENAME runs_partitioned -> runs.
6. Re-create indexes (tenant_id, global_run_id, started_at).
7. Re-apply RLS: ENABLE, FORCE, DROP/CREATE policy.
8. Re-apply grants.

Downgrade
---------
1. Create ``runs_unpartitioned`` (same schema, no PARTITION BY).
2. Copy data from partitioned ``runs``.
3. DROP TABLE runs (cascades all child partitions).
4. RENAME runs_unpartitioned -> runs.
5. Re-create indexes.
6. Re-apply RLS.
7. Re-apply grants.

Implementation status
---------------------
**works today** — Strategy A implemented in this migration.
Full 10K x 1M benchmark still deferred (hardware-gated per ADR-0051).
ADR-0051 accepted on smoke-test evidence; see bench/rls_partition_pruning/REPORT.md.

Note on ``from alembic import op``
-----------------------------------
The import is deferred to the function bodies so this module can be imported
by the test suite without an active Alembic migration context.  The local
``alembic/`` directory at the project root (a separate migrations namespace)
would shadow the installed ``alembic`` package at module scope.  Deferring
the import sidesteps the namespace collision and is harmless — Alembic's
``op`` proxy is only meaningful when called inside an upgrade/downgrade run.

Revision ID: v002
Revises: v001
Create Date: 2026-05-19
"""
from __future__ import annotations

from typing import Any

revision = "v002"
down_revision = "v001"
branch_labels = None
depends_on = None

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Quarterly partition definitions: (suffix, from_date, to_date)
_QUARTERLY_PARTITIONS: list[tuple[str, str, str]] = [
    ("y2024q1", "2024-01-01", "2024-04-01"),
    ("y2024q2", "2024-04-01", "2024-07-01"),
    ("y2024q3", "2024-07-01", "2024-10-01"),
    ("y2024q4", "2024-10-01", "2025-01-01"),
    ("y2025q1", "2025-01-01", "2025-04-01"),
    ("y2025q2", "2025-04-01", "2025-07-01"),
    ("y2025q3", "2025-07-01", "2025-10-01"),
    ("y2025q4", "2025-10-01", "2026-01-01"),
    ("y2026q1", "2026-01-01", "2026-04-01"),
    ("y2026q2", "2026-04-01", "2026-07-01"),
    ("y2026q3", "2026-07-01", "2026-10-01"),
    ("y2026q4", "2026-10-01", "2027-01-01"),
    ("y2027q1", "2027-01-01", "2027-04-01"),
    ("y2027q2", "2027-04-01", "2027-07-01"),
    ("y2027q3", "2027-07-01", "2027-10-01"),
    ("y2027q4", "2027-10-01", "2028-01-01"),
]

# ---------------------------------------------------------------------------
# Internal helpers (all take ``op`` as a parameter to avoid module-level import)
# ---------------------------------------------------------------------------


def _create_partitioned_runs(op: Any, table_name: str) -> None:
    """Create the partitioned runs table with quarterly child tables."""
    op.execute(
        f"""
        CREATE TABLE {table_name} (
            run_id            UUID        NOT NULL,
            tenant_id         UUID        NOT NULL,
            event_type        TEXT,
            global_run_id     UUID,
            started_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            status            TEXT        NOT NULL DEFAULT 'pending',
            world_size        BIGINT,
            expected_children BIGINT,
            children_arrived  BIGINT      NOT NULL DEFAULT 0,
            CONSTRAINT {table_name}_pkey PRIMARY KEY (run_id, started_at)
        ) PARTITION BY RANGE (started_at)
        """  # noqa: S608
    )
    for suffix, from_date, to_date in _QUARTERLY_PARTITIONS:
        op.execute(
            f"""
            CREATE TABLE runs_{suffix} PARTITION OF {table_name}
                FOR VALUES FROM ('{from_date}') TO ('{to_date}')
            """  # noqa: S608
        )


def _create_unpartitioned_runs(op: Any, table_name: str) -> None:
    """Create an un-partitioned runs table (used for downgrade)."""
    op.execute(
        f"""
        CREATE TABLE {table_name} (
            run_id            UUID        NOT NULL,
            tenant_id         UUID        NOT NULL,
            event_type        TEXT,
            global_run_id     UUID,
            started_at        TIMESTAMPTZ,
            status            TEXT        NOT NULL DEFAULT 'pending',
            world_size        BIGINT,
            expected_children BIGINT,
            children_arrived  BIGINT      NOT NULL DEFAULT 0,
            CONSTRAINT {table_name}_pkey PRIMARY KEY (run_id, tenant_id)
        )
        """  # noqa: S608
    )


def _copy_runs_up(op: Any, src: str, dst: str) -> None:
    """Copy rows from un-partitioned src to partitioned dst.

    COALESCE(started_at, NOW()) ensures the partition key is non-null.
    ON CONFLICT DO NOTHING is safe for idempotent re-runs.
    """
    op.execute(
        f"""
        INSERT INTO {dst}
            (run_id, tenant_id, event_type, global_run_id,
             started_at, status, world_size, expected_children, children_arrived)
        SELECT
            run_id, tenant_id, event_type, global_run_id,
            COALESCE(started_at, NOW()), status, world_size, expected_children, children_arrived
        FROM {src}
        ON CONFLICT DO NOTHING
        """  # noqa: S608
    )


def _copy_runs_down(op: Any, src: str, dst: str) -> None:
    """Copy rows from partitioned src to un-partitioned dst.

    ON CONFLICT DO NOTHING is safe for idempotent re-runs.
    """
    op.execute(
        f"""
        INSERT INTO {dst}
            (run_id, tenant_id, event_type, global_run_id,
             started_at, status, world_size, expected_children, children_arrived)
        SELECT
            run_id, tenant_id, event_type, global_run_id,
            started_at, status, world_size, expected_children, children_arrived
        FROM {src}
        ON CONFLICT DO NOTHING
        """  # noqa: S608
    )


def _create_indexes(op: Any, table_name: str) -> None:
    """Create supporting indexes on the (possibly partitioned) table."""
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {table_name}_tenant_id_idx ON {table_name} (tenant_id)"  # noqa: S608
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {table_name}_global_run_id_idx ON {table_name} (global_run_id)"  # noqa: S608
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {table_name}_started_at_idx ON {table_name} (started_at DESC)"  # noqa: S608
    )


def _apply_rls(op: Any, table_name: str) -> None:
    """Enable and force RLS; create the tenant_isolation policy."""
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")  # noqa: S608
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")  # noqa: S608
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table_name}")  # noqa: S608
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table_name}
            USING (
                tenant_id = current_setting('app.current_tenant_id')::uuid
            )
            WITH CHECK (
                tenant_id = current_setting('app.current_tenant_id')::uuid
            )
        """  # noqa: S608
    )


def _apply_grants(op: Any, table_name: str) -> None:
    """Grant SELECT/INSERT/UPDATE to novafabric_app; ALL to novafabric_migrator."""
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON TABLE {table_name} TO novafabric_app"  # noqa: S608
    )
    op.execute(
        f"GRANT ALL PRIVILEGES ON TABLE {table_name} TO novafabric_migrator"  # noqa: S608
    )


# ---------------------------------------------------------------------------
# Alembic upgrade / downgrade
# ---------------------------------------------------------------------------


def upgrade() -> None:
    """Convert runs to a quarterly range-partitioned table (Strategy A, ADR-0051)."""
    # Deferred import — see module docstring for rationale.
    from alembic import op  # noqa: PLC0415

    # Step 1: Create the new partitioned table (with child partitions).
    _create_partitioned_runs(op, "runs_partitioned")

    # Step 2: Copy data from the old un-partitioned table.
    # COALESCE(started_at, NOW()) handles the nullable started_at from v001 —
    # the partitioned table requires a non-null value so it can route to a child.
    _copy_runs_up(op, "runs", "runs_partitioned")

    # Step 3: Drop the old un-partitioned table.
    op.execute("DROP TABLE runs")  # noqa: S608

    # Step 4: Rename the new table into place.
    op.execute("ALTER TABLE runs_partitioned RENAME TO runs")  # noqa: S608

    # Step 5: Rename the per-partition primary key constraint.
    # Postgres keeps the original name on table rename.
    op.execute(
        "ALTER TABLE runs RENAME CONSTRAINT runs_partitioned_pkey TO runs_pkey"  # noqa: S608
    )

    # Step 6: Create indexes on the parent (propagates to children automatically
    # for declarative partitioned tables on Postgres 11+).
    _create_indexes(op, "runs")

    # Step 7: Apply RLS.
    _apply_rls(op, "runs")

    # Step 8: Apply grants.
    _apply_grants(op, "runs")


def downgrade() -> None:
    """Revert runs to a plain un-partitioned table (inverse of Strategy A)."""
    # Deferred import — see module docstring for rationale.
    from alembic import op  # noqa: PLC0415

    # Step 1: Create the new un-partitioned table.
    _create_unpartitioned_runs(op, "runs_unpartitioned")

    # Step 2: Copy data from the partitioned table.
    _copy_runs_down(op, "runs", "runs_unpartitioned")

    # Step 3: Drop the partitioned table (cascades all child partitions).
    op.execute("DROP TABLE runs")  # noqa: S608

    # Step 4: Rename the plain table into place.
    op.execute("ALTER TABLE runs_unpartitioned RENAME TO runs")  # noqa: S608
    op.execute(
        "ALTER TABLE runs RENAME CONSTRAINT runs_unpartitioned_pkey TO runs_pkey"  # noqa: S608
    )

    # Step 5: Create indexes.
    _create_indexes(op, "runs")

    # Step 6: Apply RLS.
    _apply_rls(op, "runs")

    # Step 7: Apply grants.
    _apply_grants(op, "runs")
