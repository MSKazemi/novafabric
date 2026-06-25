"""Initial Postgres schema for MetadataStore — production.

Revision ID: v001
Revises:
Create Date: 2026-05-13

Security invariants enforced by this migration
----------------------------------------------
1. ``novafabric_app`` role — NOLOGIN, NOBYPASSRLS.
   All application connections authenticate as a role that grants membership;
   the role itself cannot bypass RLS policies.
2. ``novafabric_migrator`` role — NOLOGIN, BYPASSRLS.
   Alembic (and the DBA running migrations) must be a member of this role so
   that schema DDL is not blocked by the tenant-isolation policies.
3. ENABLE ROW LEVEL SECURITY on every tenant-scoped table.
4. FORCE ROW LEVEL SECURITY on every tenant-scoped table — ensures the table
   owner cannot inadvertently bypass policies (FR-07, gap-006).
5. ``tenant_isolation`` policy on each table — filters and enforces that
   ``tenant_id`` equals ``current_setting('app.current_tenant_id')::uuid``.
   The GUC must be set with SET LOCAL inside a transaction
   (see PostgresMetadataStore.begin_tenant_context).

References
----------
- ADR-0040: Production MetadataStore interface
- FR-07: FORCE ROW LEVEL SECURITY requirement
- gap-006: Cross-tenant GUC leak via pgBouncer session pooling
"""
from __future__ import annotations

from alembic import op

revision = "v001"
down_revision = None
branch_labels = None
depends_on = None

# Tables that are scoped to a tenant_id and must have RLS enforced.
_TENANT_TABLES = ("runs", "capsules", "signatures", "retention_policies")


def upgrade() -> None:
    # ------------------------------------------------------------------
    # Roles
    # ------------------------------------------------------------------
    # CREATE ROLE IF NOT EXISTS requires Postgres 9.6+.
    # NOBYPASSRLS is the default for new roles but spelled out for clarity.
    op.execute(
        "DO $$ BEGIN "
        "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'novafabric_app') THEN "
        "    CREATE ROLE novafabric_app NOLOGIN NOBYPASSRLS; "
        "  END IF; "
        "END $$"
    )
    op.execute(
        "DO $$ BEGIN "
        "  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'novafabric_migrator') THEN "
        "    CREATE ROLE novafabric_migrator NOLOGIN BYPASSRLS; "
        "  END IF; "
        "END $$"
    )

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id            UUID        NOT NULL,
            tenant_id         UUID        NOT NULL,
            event_type        TEXT,
            global_run_id     UUID,
            started_at        TIMESTAMPTZ,
            status            TEXT        NOT NULL DEFAULT 'pending',
            world_size        BIGINT,
            expected_children BIGINT,
            children_arrived  BIGINT      NOT NULL DEFAULT 0,
            PRIMARY KEY (run_id, tenant_id)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS capsules (
            capsule_uri TEXT NOT NULL PRIMARY KEY,
            run_id      UUID NOT NULL,
            tenant_id   UUID NOT NULL
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS signatures (
            run_id         UUID NOT NULL,
            tenant_id      UUID NOT NULL,
            signature_hash TEXT NOT NULL,
            payload_json   JSONB NOT NULL,
            PRIMARY KEY (run_id, signature_hash)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS retention_policies (
            tenant_id   UUID  NOT NULL PRIMARY KEY,
            policy_json JSONB NOT NULL
        )
        """
    )

    # ------------------------------------------------------------------
    # Row Level Security
    # ------------------------------------------------------------------
    for table in _TENANT_TABLES:
        # Enable RLS — no-op if already enabled.
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")  # noqa: S608
        # FORCE RLS applies policies even to the table owner (FR-07).
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")  # noqa: S608
        # Tenant-isolation policy: only rows whose tenant_id equals the
        # transaction-scoped GUC are visible or writable.
        # DROP before CREATE so the migration is idempotent on re-runs.
        op.execute(
            f"DROP POLICY IF EXISTS tenant_isolation ON {table}"  # noqa: S608
        )
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (
                    tenant_id = current_setting('app.current_tenant_id')::uuid
                )
                WITH CHECK (
                    tenant_id = current_setting('app.current_tenant_id')::uuid
                )
            """  # noqa: S608
        )

    # ------------------------------------------------------------------
    # Grants
    # ------------------------------------------------------------------
    for table in _TENANT_TABLES:
        op.execute(
            f"GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO novafabric_app"  # noqa: S608
        )
        op.execute(
            f"GRANT ALL PRIVILEGES ON TABLE {table} TO novafabric_migrator"  # noqa: S608
        )


def downgrade() -> None:
    for table in reversed(_TENANT_TABLES):
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")  # noqa: S608
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM novafabric_app")  # noqa: S608
        op.execute(f"REVOKE ALL PRIVILEGES ON TABLE {table} FROM novafabric_migrator")  # noqa: S608
        op.execute(f"DROP TABLE IF EXISTS {table}")  # noqa: S608

    # Roles are intentionally NOT dropped on downgrade — a role may be
    # referenced by other objects in the same database.  Remove manually
    # if required.
