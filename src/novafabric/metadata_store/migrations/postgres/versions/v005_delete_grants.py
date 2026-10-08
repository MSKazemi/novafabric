"""Grant index-delete privileges to the application role (ADR-0206 P2).

Revision ID: v005
Revises: v004
Create Date: 2026-10-08

ADR-0206 P2 added MetadataStore.delete_run(), which deletes derived index rows
from runs, capsules and signatures. The production application role remained on
the pre-delete grant set (SELECT, INSERT, UPDATE), so a real NOBYPASSRLS
connection could not execute the new operation even though superuser-backed
tests could.

This migration grants only DELETE on the three tables used by delete_run().
retention_policies is intentionally unchanged.
"""
from __future__ import annotations

from alembic import op

revision = "v005"
down_revision = "v004"
branch_labels = None
depends_on = None

_DELETE_TABLES = ("runs", "capsules", "signatures")


def upgrade() -> None:
    for table in _DELETE_TABLES:
        op.execute(
            f"GRANT DELETE ON TABLE {table} TO novafabric_app"  # noqa: S608
        )


def downgrade() -> None:
    for table in reversed(_DELETE_TABLES):
        op.execute(
            f"REVOKE DELETE ON TABLE {table} FROM novafabric_app"  # noqa: S608
        )
