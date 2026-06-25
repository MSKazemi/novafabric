"""Add site_id column to lineage_edges (Postgres).

Revision ID: p0003
Revises: p0002
Create Date: 2026-05-13
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "p0003"
down_revision: str | None = "p0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    """Add site_id column idempotently."""
    conn = op.get_bind()
    result = conn.execute(sa.text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='lineage_edges' AND column_name='site_id'"
    ))
    if result.fetchone() is None:
        op.add_column(
            "lineage_edges",
            sa.Column("site_id", sa.Text(), nullable=False, server_default="local"),
        )
        op.create_index("ix_lineage_edges_site_id", "lineage_edges", ["site_id"])


def downgrade() -> None:
    """Drop site_id column; refuse if non-local data would be lost."""
    conn = op.get_bind()
    result = conn.execute(
        sa.text("SELECT COUNT(*) FROM lineage_edges WHERE site_id != 'local'")
    )
    count = result.scalar()
    if count:
        raise RuntimeError(
            f"Cannot downgrade: {count} edges have non-local site_id values. "
            "Migration data would be permanently lost."
        )
    op.drop_column("lineage_edges", "site_id")
