"""Put ``tenant_id`` back in the ``runs`` primary key (RFC-0001, option C).

v002 partitioned ``runs`` by ``started_at``. Postgres requires the partition key
to be part of any unique constraint, so the primary key became
``(run_id, started_at)`` and ``tenant_id`` fell out of it. Both halves were
individually correct and together they broke server mode:
``PostgresMetadataStore.register_run()`` writes ``ON CONFLICT (run_id,
tenant_id)``, which cannot be inferred against a key that does not contain
``tenant_id``, so **every insert raised** ``InvalidColumnReference`` once the
migrations reached head (issue #23).

RFC-0001 chose option C: widen the key to ``(run_id, tenant_id, started_at)``.
It is the only option that keeps ``tenant_id`` *structurally* in the key rather
than incidentally, which matters because ``runs`` is the RLS-protected,
tenant-scoped table — the one place where "the tenant column is part of the
identity" should not be an implementation detail.

**What this does not do.** A three-column key cannot enforce uniqueness on
``(run_id, tenant_id)`` alone; no key on a range-partitioned table can, because
the partition column must be included. Application-level idempotency therefore
moves into ``register_run`` (an insert guarded by ``WHERE NOT EXISTS``), and the
residual race is documented in ADR-0226 rather than pretended away.

The table swap mirrors v002's: build alongside, copy, drop, rename, then
re-apply indexes, RLS and grants — because a rename does not carry policies.
"""

from __future__ import annotations

from typing import Any

revision = "v004"
down_revision = "v003"
branch_labels = None
depends_on = None

# Same quarterly coverage as v002. Kept in step deliberately: a partition set
# that disagrees with v002's would silently drop rows during the copy.
_QUARTERLY_PARTITIONS: list[tuple[str, str, str]] = [
    (f"y{year}q{q}", f"{year}-{m:02d}-01", f"{year + (q == 4)}-{(m + 3 - 1) % 12 + 1:02d}-01")
    for year in (2024, 2025, 2026, 2027)
    for q, m in ((1, 1), (2, 4), (3, 7), (4, 10))
]


def _create_runs(op: Any, table_name: str, *, pkey_columns: str) -> None:
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
            CONSTRAINT {table_name}_pkey PRIMARY KEY ({pkey_columns})
        ) PARTITION BY RANGE (started_at)
        """  # noqa: S608
    )
    for suffix, from_date, to_date in _QUARTERLY_PARTITIONS:
        op.execute(
            f"""
            CREATE TABLE {table_name}_{suffix} PARTITION OF {table_name}
                FOR VALUES FROM ('{from_date}') TO ('{to_date}')
            """  # noqa: S608
        )
    # A DEFAULT partition so a run whose start time falls outside the declared
    # quarters is stored rather than rejected. v002 had none, so a timestamp
    # before 2024 or after 2027 failed with "no partition of relation found for
    # row" — a cliff the calendar walks into on its own.
    op.execute(
        f"CREATE TABLE {table_name}_default PARTITION OF {table_name} DEFAULT"  # noqa: S608
    )


def _apply_indexes(op: Any, table_name: str) -> None:
    for column, expr in (
        ("tenant_id", "(tenant_id)"),
        ("global_run_id", "(global_run_id)"),
        ("started_at", "(started_at DESC)"),
    ):
        op.execute(
            f"CREATE INDEX IF NOT EXISTS {table_name}_{column}_idx ON {table_name} {expr}"  # noqa: S608
        )
    # Supports register_run's WHERE NOT EXISTS probe and lookup_run, both of
    # which filter on the pair. Not unique — see the module docstring.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {table_name}_run_tenant_idx "  # noqa: S608
        f"ON {table_name} (run_id, tenant_id)"
    )


def _apply_rls(op: Any, table_name: str) -> None:
    op.execute(f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY")  # noqa: S608
    op.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")  # noqa: S608
    op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table_name}")  # noqa: S608
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON {table_name}
            USING (tenant_id = current_setting('app.current_tenant_id')::uuid)
            WITH CHECK (tenant_id = current_setting('app.current_tenant_id')::uuid)
        """  # noqa: S608
    )


def _apply_grants(op: Any, table_name: str) -> None:
    op.execute(
        f"GRANT SELECT, INSERT, UPDATE ON TABLE {table_name} TO novafabric_app"  # noqa: S608
    )
    op.execute(
        f"GRANT ALL PRIVILEGES ON TABLE {table_name} TO novafabric_migrator"  # noqa: S608
    )


_COLUMNS = (
    "run_id, tenant_id, event_type, global_run_id, started_at, "
    "status, world_size, expected_children, children_arrived"
)


def _swap(op: Any, *, pkey_columns: str, dedupe_on: str) -> None:
    """Rebuild ``runs`` with *pkey_columns* as its primary key, preserving rows.

    *dedupe_on* must cover the key being enforced. Getting this wrong is not
    theoretical: deduping on ``(run_id, tenant_id)`` protects the upgrade but not
    the downgrade, whose target key is ``(run_id, started_at)`` — two tenants
    sharing a timestamp then collide and the copy aborts halfway. Caught by
    ``test_v004_downgrade_returns_to_the_narrow_key``.
    """
    op.execute("DROP TABLE IF EXISTS runs_rekeyed CASCADE")
    _create_runs(op, "runs_rekeyed", pkey_columns=pkey_columns)

    # DISTINCT ON collapses any duplicate the looser key allowed to exist,
    # keeping the earliest row rather than aborting the migration.
    op.execute(
        f"""
        INSERT INTO runs_rekeyed ({_COLUMNS})
        SELECT DISTINCT ON ({dedupe_on}) {_COLUMNS}
        FROM runs
        ORDER BY {dedupe_on}, started_at
        """  # noqa: S608
    )

    op.execute("DROP TABLE runs CASCADE")
    op.execute("ALTER TABLE runs_rekeyed RENAME TO runs")
    for suffix, _, _ in _QUARTERLY_PARTITIONS:
        op.execute(f"ALTER TABLE runs_rekeyed_{suffix} RENAME TO runs_{suffix}")
    op.execute("ALTER TABLE runs_rekeyed_default RENAME TO runs_default")
    op.execute("ALTER TABLE runs RENAME CONSTRAINT runs_rekeyed_pkey TO runs_pkey")

    _apply_indexes(op, "runs")
    _apply_rls(op, "runs")
    _apply_grants(op, "runs")


def upgrade() -> None:
    """Widen the runs primary key to (run_id, tenant_id, started_at)."""
    from alembic import op  # noqa: PLC0415

    # Dedupe on the pair, not the triple: the new key permits two rows with the
    # same run and tenant at different timestamps, but `register_run`'s contract
    # does not. Collapsing them restores the invariant while migrating.
    _swap(op, pkey_columns="run_id, tenant_id, started_at", dedupe_on="run_id, tenant_id")


def downgrade() -> None:
    """Return to v002's (run_id, started_at) key.

    Lossy in principle: two tenants that legitimately share a ``run_id`` at the
    same ``started_at`` cannot both survive the narrower key. ``DISTINCT ON``
    keeps one and drops the other rather than aborting, which is the documented
    behaviour of this downgrade — do not run it on a multi-tenant deployment
    without checking for that collision first.
    """
    from alembic import op  # noqa: PLC0415

    _swap(op, pkey_columns="run_id, started_at", dedupe_on="run_id, started_at")
